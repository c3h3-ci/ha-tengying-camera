/*
 * bridge_ppcs.c — 腾影(PPCS) -> H.264 Annex-B on stdout
 *
 * 完全基于脱壳 Java 源码 Cs2Camera.java / Camera.java / AVIOCTRLDEFs.java
 * 复刻的 PPCS 客户端。连接序列（与 App 一致）：
 *
 *   1. PPCS_Initialize(JSON)         全局 InitString + SessAliveSec + MaxNumSess
 *   2. PPCS_ConnectByServer(p2pid, mode=123, 0, initstring)
 *   3. PPCS_Write(sid, ch0, 32770 密码包)    60B payload, 密码放 [8..]
 *   4. PPCS_Write(sid, ch0, 511 启流包)      [channel=2][mode=0]
 *   5. PPCS_Write(sid, ch0, 768 音频包)      [channel=1][mode=0]
 *   6. PPCS_Write(sid, ch0, 800 清晰度包)    [channel=0][quality=1]
 *   7. PPCS_Read(sid, ch0, buf, &len, 200ms) 循环收流
 *
 * 收流帧格式（16B 头）：[0]codec [1]subType [2]flags [5..7]序号LE24
 *                        [8..11]帧长LE32 [12..15]时间戳
 * 视频帧：codec==78(H264) && subType==0，数据为含 00 00 00 01 起始码的 NAL。
 *
 * 用法: ./bridge_ppcs <p2pid> <pwd> <initstring> [mode] [ctrl_port] [audio_down_port]
 *   p2pid      blob 解出的 p2pid（含逗号后缀也可，原样传入）
 *   pwd        blob 解出的 pwd（≤48 字节）
 *   initstring blob 解出的 initstring，去掉 "ppcs:" 前缀
 *   mode       连接模式，默认 0 (禁 LAN 搜索, 走云中继)
 *   ctrl_port  PTZ/指令控制端口（127.0.0.1），默认 0=不启用
 *   audio_down_port 音频下行推送端口（127.0.0.1），默认 0=不启用
 *              启用后: 独立线程读 ch1 音频帧，连接该端口的客户端
 *              持续收到原始帧 [16B头][G711A payload]（双向语音下行）。
 *
 * 依赖: libPPCS_API.so(原版) + liblog.so(假) + bionic_shim(静态内嵌)
 * 运行: LD_LIBRARY_PATH=. ./bridge_ppcs <p2pid> <pwd> <initstring>
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <dlfcn.h>
#include <time.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <time.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <pthread.h>

/* App 级全局 InitString（来自 Cs2Camera.P2pCs2InitStr，常量） */
#define APP_INIT_STR "EEGDFHBIKAJNGBJNEMGNFPECHPNFHGNMGCFEBOCGAAJHLNKJDJAPCEPMGHLGJBLNAJMOKCDKONMDBJCCJHMD"
#define CH_CMD_DEFAULT 123   /* 云连接模式 */
#define VIDEO_BUF_SIZE 2048
#define READ_TIMEOUT_MS 1000

/* ---- PPCS API 函数指针（dlopen 动态解析） ---- */
static int (*p_Initialize)(const unsigned char *json);
static int (*p_ConnectByServer)(const char *p2pid, signed char mode, int timeout, const char *initstr);
static int (*p_Read)(int sid, signed char ch, unsigned char *buf, int *len, int timeout_ms);
static int (*p_Write)(int sid, signed char ch, const unsigned char *buf, int len);
static int (*p_Close)(int sid);
static int (*p_DeInitialize)(void);
static int (*p_GetAPIVersion)(void);
static int (*p_Check)(int sid, void *session);
static int (*p_SetLogfile)(const char *path);
static int (*p_PktSend)(int sid, signed char ch, const unsigned char *buf, int len);

static volatile int g_running = 1;
static int g_ctrl_port = 0;   /* PTZ 控制端口（0=不启用） */
static int g_audio_down_port = 0; /* 音频下行推送端口（0=不启用） */
static int g_sid = -1;        /* 当前会话 ID（控制线程用） */

/* 前向声明（audio_enqueue 使用，定义在后方） */
static void put_le32(uint8_t *p, uint32_t v);
static uint32_t get_le32(const uint8_t *p);

/* ---- 音频下行（双向语音：设备麦克风 → HA）----
 * App 逆向（d.java / Camera.chIndexForRecvAudio=1）确认：
 *   音频下行通道 = ch1；帧 = [16B 头][payload]
 *   头: [codec_id:LE16=138(G711A)|134][flags][...][size:LE32@8][ts:LE32@12]
 *   先 PPCS_Read(ch1,16) 读头，再 PPCS_Read(ch1,size) 读 payload。
 * 实现: 独立线程 audio_recv_thread 读 ch1 → 环形缓冲 →
 *       audio_down_server 监听 127.0.0.1:{g_audio_down_port}，
 *       每个连接客户端持续推送原始帧（[16B头][payload]）。
 */
#define AUDIO_CHANNEL 1
#define ARING_SIZE (512 * 1024)
#define ARING_MASK (ARING_SIZE - 1)
static uint8_t g_aring[ARING_SIZE];
static size_t g_aring_head = 0;   /* 写入位置 */
static size_t g_aring_tail = 0;   /* 最旧有效帧起点 */
static pthread_mutex_t g_aring_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_aring_cond = PTHREAD_COND_INITIALIZER;

/* 环形缓冲帧格式: [flen:LE32][16B 头 + payload]（flen = 16+plen，定界用） */
static size_t aring_used(void)
{
    if (g_aring_head >= g_aring_tail) return g_aring_head - g_aring_tail;
    return ARING_SIZE - (g_aring_tail - g_aring_head);
}

static uint32_t aring_peek_len(size_t pos)
{
    uint8_t b[4];
    for (int i = 0; i < 4; i++) b[i] = g_aring[(pos + (size_t)i) & ARING_MASK];
    return get_le32(b);
}

static void aring_skip_one(void)
{
    uint32_t flen = aring_peek_len(g_aring_tail);
    g_aring_tail = (g_aring_tail + 4 + flen) & ARING_MASK;
}

static void audio_enqueue(const uint8_t *hdr16, const uint8_t *payload, int plen)
{
    int flen = 16 + plen;
    pthread_mutex_lock(&g_aring_mtx);
    /* 空间不足丢最旧帧（保留至少一帧，避免空转） */
    while (g_aring_tail != g_aring_head &&
           aring_used() + (size_t)flen + 4 > ARING_SIZE)
        aring_skip_one();
    /* 写入 [flen:LE32] */
    uint8_t lb[4];
    put_le32(lb, (uint32_t)flen);
    for (int i = 0; i < 4; i++)
    {
        g_aring[g_aring_head] = lb[i];
        g_aring_head = (g_aring_head + 1) & ARING_MASK;
    }
    /* 写入 [16B 头 + payload]（跨环回时两段写） */
    for (int i = 0; i < 16; i++)
    {
        g_aring[g_aring_head] = hdr16[i];
        g_aring_head = (g_aring_head + 1) & ARING_MASK;
    }
    for (int i = 0; i < plen; i++)
    {
        g_aring[g_aring_head] = payload[i];
        g_aring_head = (g_aring_head + 1) & ARING_MASK;
    }
    pthread_cond_broadcast(&g_aring_cond);
    pthread_mutex_unlock(&g_aring_mtx);
}

/* ---- AVIOCTRL 查询响应缓冲（recv_loop 存 / ctrl_server wait 取） ----
 * 设备对查询指令（32790 等 REQ）回 RESP（REQ+1），recv_loop 收到后存入，
 * ctrl_server 处理 {"io":N,"payload":...,"wait":1} 时同步等待并回传。
 */
static pthread_mutex_t g_resp_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_resp_cond = PTHREAD_COND_INITIALIZER;
static uint32_t g_resp_io = 0;
static int      g_resp_len = 0;
static uint8_t  g_resp_buf[2048];

static void resp_store(uint32_t ioType, const uint8_t *payload, int plen)
{
    pthread_mutex_lock(&g_resp_mtx);
    g_resp_io = ioType;
    g_resp_len = plen > (int)sizeof(g_resp_buf) ? (int)sizeof(g_resp_buf) : plen;
    if (g_resp_len > 0 && payload)
        memcpy(g_resp_buf, payload, (size_t)g_resp_len);
    pthread_cond_broadcast(&g_resp_cond);
    pthread_mutex_unlock(&g_resp_mtx);
}

/* 等待设备响应（最多 timeout_ms），返回 0=有响应 / -1=超时 */
static int resp_wait(uint32_t *ioType, uint8_t *out, int out_max, int *out_len, int timeout_ms)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout_ms / 1000;
    ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L)
    {
        ts.tv_sec += 1;
        ts.tv_nsec -= 1000000000L;
    }
    pthread_mutex_lock(&g_resp_mtx);
    int have = (g_resp_len > 0);
    if (!have)
        pthread_cond_timedwait(&g_resp_cond, &g_resp_mtx, &ts);
    have = (g_resp_len > 0);
    if (have)
    {
        *ioType = g_resp_io;
        *out_len = g_resp_len > out_max ? out_max : g_resp_len;
        memcpy(out, g_resp_buf, (size_t)*out_len);
        g_resp_len = 0;   /* 消费后清空 */
        pthread_mutex_unlock(&g_resp_mtx);
        return 0;
    }
    pthread_mutex_unlock(&g_resp_mtx);
    return -1;
}

static void *ctrl_server(void *arg);   /* 前向声明（定义在 main 后） */

static void on_signal(int sig)
{
    (void)sig;
    g_running = 0;
}

/* ---- 小端工具 ---- */
static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xff);
    p[1] = (uint8_t)((v >> 8) & 0xff);
    p[2] = (uint8_t)((v >> 16) & 0xff);
    p[3] = (uint8_t)((v >> 24) & 0xff);
}

static uint32_t get_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t get_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

/* ---- IOCTRL 包: [ioType:LE32][size:LE32][payload] ---- */
static size_t build_ioctrl(uint8_t *out, uint32_t ioType,
                           const uint8_t *payload, size_t payload_len)
{
    put_le32(out, ioType);
    put_le32(out + 4, (uint32_t)payload_len);
    if (payload && payload_len)
        memcpy(out + 8, payload, payload_len);
    return 8 + payload_len;
}

/* ---- dlopen bionic shim + libPPCS_API.so ----
 * 顺序关键：先以 RTLD_GLOBAL 加载 libbionic_shim.so（提供 __errno/FORTIFY
 * 符号），再加载 libPPCS_API.so——动态链接器会从已加载的全局符号表
 * 解析后者的 UNDEF（标准 POSIX 来自 libc.so，bionic 专有符号来自 shim）。 */
static int load_ppcs(void)
{
    void *shim = dlopen("libbionic_shim.so", RTLD_NOW | RTLD_GLOBAL);
    if (!shim)
    {
        fprintf(stderr, "[load] dlopen libbionic_shim.so failed: %s\n", dlerror());
        /* shim 缺失时继续尝试，便于在纯 glibc 环境（符号原生存在）冒烟 */
        fprintf(stderr, "[load] continue without bionic shim...\n");
    }

    void *h = dlopen("libPPCS_API.so", RTLD_NOW | RTLD_GLOBAL);
    if (!h)
    {
        fprintf(stderr, "[load] dlopen libPPCS_API.so failed: %s\n", dlerror());
        return -1;
    }
#define LOAD(var, sym)                                                     \
    do                                                                     \
    {                                                                      \
        *(void **)&p_##var = dlsym(h, sym);                                \
        if (!p_##var)                                                      \
        {                                                                  \
            fprintf(stderr, "[load] missing symbol: %s\n", sym);           \
            return -1;                                                     \
        }                                                                  \
    } while (0)
    LOAD(Initialize, "PPCS_Initialize");
    LOAD(ConnectByServer, "PPCS_ConnectByServer");
    LOAD(Read, "PPCS_Read");
    LOAD(Write, "PPCS_Write");
    LOAD(PktSend, "PPCS_PktSend");
    LOAD(Close, "PPCS_Close");
    LOAD(DeInitialize, "PPCS_DeInitialize");
    LOAD(GetAPIVersion, "PPCS_GetAPIVersion");
    LOAD(Check, "PPCS_Check");
    /* Set_Logfile 是可选（so 内部日志），失败不阻断 */
    p_SetLogfile = dlsym(h, "Set_Logfile");
    if (p_SetLogfile)
        p_SetLogfile("/tmp/ppcs.log");
#undef LOAD
    return 0;
}

/* ---- PPCS_Initialize（App 级全局初始化） ---- */
static int ppcs_init(void)
{
    char json[512];
    snprintf(json, sizeof(json),
             "{\"InitString\":\"%s\",\"SessAliveSec\":8,\"MaxNumSess\":128}",
             APP_INIT_STR);
    int ret = p_Initialize((const unsigned char *)json);
    fprintf(stderr, "[init] PPCS_Initialize -> %d (%s)\n", ret,
            (ret == 0 || ret == -2) ? "OK" : "FAIL");
    return (ret == 0 || ret == -2) ? 0 : -1;
}

/* ---- 获取第一个非 loopback 的 IPv4 网卡名（LAN 搜索 BindInterface 用） ---- */
static int get_first_ipv4_iface(char *buf, size_t n)
{
    struct ifaddrs *ifa0 = NULL, *ifa;
    int found = 0;
    if (getifaddrs(&ifa0) != 0)
        return -1;
    for (ifa = ifa0; ifa; ifa = ifa->ifa_next)
    {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET)
            continue;
        if (ifa->ifa_flags & IFF_LOOPBACK)
            continue;
        if (ifa->ifa_name && ifa->ifa_name[0])
        {
            snprintf(buf, n, "%s", ifa->ifa_name);
            found = 1;
            break;
        }
    }
    freeifaddrs(ifa0);
    return found ? 0 : -1;
}

/* ---- PPCS_ConnectByServer ----
 * 云连接（默认）：initstring 直接传裸串。
 * LAN 搜索（mode=63）：Java 里包成 {"InitString":"...","BindInterface":"<iface>"} JSON。 */
static int ppcs_connect(const char *p2pid, const char *initstring, int mode)
{
    char lan_init[1024];
    const char *connect_init = initstring;
    if (mode == 63)   /* CH_CMD_LanSearch */
    {
        char iface[64];
        if (get_first_ipv4_iface(iface, sizeof(iface)) == 0)
        {
            snprintf(lan_init, sizeof(lan_init),
                     "{\"InitString\":\"%s\",\"BindInterface\":\"%s\"}",
                     initstring, iface);
            connect_init = lan_init;
            fprintf(stderr, "[conn] LAN mode, BindInterface=%s\n", iface);
        }
        else
        {
            fprintf(stderr, "[conn] LAN mode, no iface found, connecting without BindInterface\n");
        }
    }
    int sid = p_ConnectByServer(p2pid, (signed char)mode, 0, connect_init);
    fprintf(stderr, "[conn] PPCS_ConnectByServer(%s, mode=%d) -> %d\n",
            p2pid, mode, sid);
    return sid;
}

/* ---- 发启流命令序列（32770 密码 + 511 启流 + 768 音频 + 800 清晰度） ----
 * Java sendPwd() 源码：云连接普通设备也发 32770（sendIOCtrl(32770, ...)），
 * 然后 sendPwdWithLiveShow 延时 2s 发 511/768/800。抓包曾"无 32770"
 * 系抓包遗漏（时序在连接早期）。 */
static int send_start_stream(int sid, const char *pwd)
{
    uint8_t pkt[512];
    int ret;

    /* 1. 32770 密码请求: payload 60B, [0..7]=0, [8..55]=pwd(≤48), [56..59]=0 */
    {
        uint8_t payload[60];
        memset(payload, 0, sizeof(payload));
        size_t pwlen = strlen(pwd);
        if (pwlen > 48) pwlen = 48;
        memcpy(payload + 8, pwd, pwlen);
        size_t n = build_ioctrl(pkt, 32770, payload, sizeof(payload));
        ret = p_Write(sid, 0, pkt, (int)n);
        fprintf(stderr, "[cmd] 32770 password(uid) -> %d\n", ret);
    }

    /* App: sendPwdWithLiveShow delay 2000ms 后再启流 */
    usleep(2000000);

    /* 2. 511 启流: payload [channel=2][mode=0] (avChannel=2, cameraIndex=0) */
    {
        uint8_t payload[8];
        put_le32(payload, 2);
        put_le32(payload + 4, 0);
        size_t n = build_ioctrl(pkt, 511, payload, sizeof(payload));
        ret = p_Write(sid, 0, pkt, (int)n);
        fprintf(stderr, "[cmd] 511 start(ch=2) -> %d\n", ret);
    }

    /* 3. 768 音频: payload [channel=1][mode=0] (getAudioChannel=1) */
    {
        uint8_t payload[8];
        put_le32(payload, 1);
        put_le32(payload + 4, 0);
        size_t n = build_ioctrl(pkt, 768, payload, sizeof(payload));
        ret = p_Write(sid, 0, pkt, (int)n);
        fprintf(stderr, "[cmd] 768 audio(ch=1) -> %d\n", ret);
    }

    /* 4. 800 清晰度: payload [channel=0][quality=1] */
    {
        uint8_t payload[8];
        put_le32(payload, 0);
        payload[4] = 1;   /* quality */
        size_t n = build_ioctrl(pkt, 800, payload, sizeof(payload));
        ret = p_Write(sid, 0, pkt, (int)n);
        fprintf(stderr, "[cmd] 800 quality(ch=0,q=1) -> %d\n", ret);
    }
    return 0;
}

/* ---- HEVC 参数集缓存（VPS/SPS/PPS） ----
 * 设备可能从 GOP 中间开始推流，ffmpeg RTSP push 需要先拿到 extradata
 * （VPS/SPS/PPS）才能 ANNOUNCE 建会话。这里把参数集缓存下来，
 * 在第一个关键帧(IDR)前前置输出，保证 ffmpeg 一开始就能解析。 */
#define PS_MAX 4096
static uint8_t g_ps_vps[PS_MAX]; static int g_ps_vps_len = 0;
static uint8_t g_ps_sps[PS_MAX]; static int g_ps_sps_len = 0;
static uint8_t g_ps_pps[PS_MAX]; static int g_ps_pps_len = 0;
static int g_ps_tracked = 0;   /* 设备推流中是否已正常输出过参数集 */
static int g_ps_sent = 0;      /* 参数集是否已输出过（前置或正常） */

static void ps_reset(void)
{
    g_ps_vps_len = g_ps_sps_len = g_ps_pps_len = 0;
    g_ps_tracked = 0;
    g_ps_sent = 0;
}

/* 从 Annex-B NAL 块解析 HEVC NAL 类型：nal 指向起始码开头 */
static int nal_hevc_type(const uint8_t *nal, int len)
{
    if (len < 4) return -1;
    int sc_len = (nal[2] == 1) ? 3 : 4;
    if (len - sc_len < 3) return -1;
    uint16_t hdr = (nal[sc_len] << 8) | nal[sc_len + 1];
    if ((hdr >> 15) != 0) return -1;   /* forbidden_zero_bit 非 0，非 HEVC */
    return (hdr >> 9) & 0x3F;
}

/* 跟踪并缓存参数集 */
static void ps_track(const uint8_t *nal, int len)
{
    int type = nal_hevc_type(nal, len);
    if (type < 0) return;
    int sc_len = (nal[2] == 1) ? 3 : 4;
    const uint8_t *body = nal + sc_len;
    int blen = len - sc_len;
    if (blen > PS_MAX) blen = PS_MAX;
    if (type == 32) { memcpy(g_ps_vps, body, blen); g_ps_vps_len = blen; }
    else if (type == 33) { memcpy(g_ps_sps, body, blen); g_ps_sps_len = blen; }
    else if (type == 34) { memcpy(g_ps_pps, body, blen); g_ps_pps_len = blen; }
}

/* 关键帧（IDR）：HEVC NAL type 19/20 */
static int is_idr_nal(const uint8_t *nal, int len)
{
    int type = nal_hevc_type(nal, len);
    return (type == 19 || type == 20);
}

/* 前置输出参数集（含 4 字节起始码） */
static void ps_emit_pre(void)
{
    if (g_ps_sent) return;
    if (!(g_ps_vps_len || g_ps_sps_len || g_ps_pps_len)) return;
    static const uint8_t sc4[4] = {0, 0, 0, 1};
    if (g_ps_vps_len) { fwrite(sc4, 1, 4, stdout); fwrite(g_ps_vps, 1, g_ps_vps_len, stdout); }
    if (g_ps_sps_len) { fwrite(sc4, 1, 4, stdout); fwrite(g_ps_sps, 1, g_ps_sps_len, stdout); }
    if (g_ps_pps_len) { fwrite(sc4, 1, 4, stdout); fwrite(g_ps_pps, 1, g_ps_pps_len, stdout); }
    fflush(stdout);
    g_ps_sent = 1;
}

/* 单个 NAL 输出决策：
 * - VPS/SPS/PPS: 缓存；若已输出过(前置)则跳过，避免 ffmpeg extradata 重复；
 *   否则正常输出并标记 tracked
 * - IDR: 若参数集既未前置输出也未正常输出过（设备从 GOP 中间推流），
 *   则把缓存的参数集前置输出，保证 ffmpeg 一开始就有 extradata
 * 返回 1 = 跳过不输出，0 = 正常输出 */
static int ps_should_skip(const uint8_t *nal, int len)
{
    int type = nal_hevc_type(nal, len);
    if (type < 0) return 0;
    if (type == 32 || type == 33 || type == 34)
    {
        ps_track(nal, len);
        if (g_ps_sent) return 1;      /* 已前置输出 → 去重跳过 */
        g_ps_tracked = 1;             /* 设备推的参数集，正常输出 */
        return 0;
    }
    if (is_idr_nal(nal, len))
    {
        if (!g_ps_sent && !g_ps_tracked &&
            (g_ps_vps_len || g_ps_sps_len || g_ps_pps_len))
        {
            ps_emit_pre();            /* 设备没推参数集 → 前置输出缓存的 */
            g_ps_sent = 1;
        }
        return 0;
    }
    return 0;
}

/* ---- 跨块 NAL 重组输出（Annex-B，兼容 H264/HEVC） ----
 * PPCS_Read 每次返回 ≤2048B 数据块，一个 NAL 可能跨多个块。
 * 用累积缓冲把跨块 NAL 拼完整再输出。 */
#define ACC_SIZE (1024 * 1024)
static uint8_t g_acc[ACC_SIZE];
static int g_acc_len = 0;

static void acc_append(const uint8_t *buf, int len)
{
    if (g_acc_len + len > ACC_SIZE)
    {
        g_acc_len = 0;   /* 缓冲满且没消费，清空防膨胀 */
    }
    memcpy(g_acc + g_acc_len, buf, len);
    g_acc_len += len;
}

static void emit_acc(void)
{
    for (;;)
    {
        int i = 0;
        while (i <= g_acc_len - 4 && !(g_acc[i] == 0 && g_acc[i + 1] == 0 &&
               (g_acc[i + 2] == 1 || (g_acc[i + 2] == 0 && g_acc[i + 3] == 1))))
            i++;
        if (i >= g_acc_len - 4)
        {
            g_acc_len = 0;   /* 无起始码，全部丢弃 */
            return;
        }
        if (i > 0)
        {
            memmove(g_acc, g_acc + i, g_acc_len - i);
            g_acc_len -= i;
        }
        int sc_len = (g_acc[2] == 1) ? 3 : 4;
        int k = sc_len;
        int found = 0;
        while (k <= g_acc_len - 4)
        {
            if (g_acc[k] == 0 && g_acc[k + 1] == 0 &&
                (g_acc[k + 2] == 1 || (g_acc[k + 2] == 0 && g_acc[k + 3] == 1)))
            {
                found = 1;
                break;
            }
            k++;
        }
        if (!found)
        {
            if (g_acc_len > ACC_SIZE - 4096)
                g_acc_len = 0;   /* 单个 NAL 异常大，清空防膨胀 */
            return;              /* NAL 跨块未完成，等待后续数据 */
        }
        if (k > sc_len)
        {
            /* 参数集去重/前置逻辑；返回 1 跳过该 NAL */
            if (!ps_should_skip(g_acc, k))
            {
                fwrite(g_acc, 1, k, stdout);
                fflush(stdout);
            }
        }
        memmove(g_acc, g_acc + k, g_acc_len - k);
        g_acc_len -= k;
    }
}

/* ---- 收流主循环 ----
 * 命令走 channel 0（sendIOCtrl 用 ch=0），视频数据在 channel 2
 * （Java: recvVideoHandler 用 avChannel=2 调 readPPCS）。
 * 轮询 ch0（IOCTRL 响应，观察设备对密码/启流的反馈）+ ch2（视频）。 */
#define VIDEO_CHANNEL 2
#define IOCTRL_CHANNEL 0

static void dump_hex(const char *tag, const uint8_t *buf, int len)
{
    fprintf(stderr, "[%s] %d bytes:", tag, len);
    int n = len < 32 ? len : 32;
    for (int i = 0; i < n; i++)
        fprintf(stderr, " %02x", buf[i]);
    fprintf(stderr, "\n");
}

static void recv_loop(int sid)
{
    time_t last_data_time = time(NULL);
    uint8_t *buf = (uint8_t *)malloc(VIDEO_BUF_SIZE);
    uint8_t *iobuf = (uint8_t *)malloc(4096);
    long n_timeout = 0, n_data = 0, n_err = 0, n_io = 0;
    if (!buf || !iobuf)
    {
        fprintf(stderr, "[recv] malloc failed\n");
        free(buf);
        free(iobuf);
        return;
    }
    ps_reset();   /* 新会话重置参数集缓存（设备参数可能变化） */
    fprintf(stderr, "[recv] pumping (SID=%d, video ch=%d, ioctrl ch=%d)...\n",
            sid, VIDEO_CHANNEL, IOCTRL_CHANNEL);
    while (g_running)
    {
        /* ch0: IOCTRL 响应（设备密码确认/就绪/错误/AVIOCTRL 查询响应） */
        int len0 = 4096;
        int ret0 = p_Read(sid, IOCTRL_CHANNEL, iobuf, &len0, 50);
        if (ret0 >= 0 && len0 > 0)
        {
            dump_hex("ioctrl", iobuf, len0);
            n_io++;
            /* AVIOCTRL 响应包: [ioType:LE32][size:LE32][payload]（>=32768 为用户级查询响应） */
            if (len0 >= 8)
            {
                uint32_t io0 = get_le32(iobuf);
                uint32_t size0 = get_le32(iobuf + 4);
                if (io0 >= 32768 && size0 <= (uint32_t)(len0 - 8))
                    resp_store(io0, iobuf + 8, (int)size0);
            }
        }
        else if (ret0 < 0 && ret0 != -3)
        {
            fprintf(stderr, "[recv] IOCTRL PPCS_Read -> %d\n", ret0);
            break;
        }

        /* 视频通道: 只读 ch2（设备主码流 HEVC 在 ch2, 2-composed 架构） */
        int got = 0;
        {
            int len = VIDEO_BUF_SIZE;
            int ret = p_Read(sid, (signed char)VIDEO_CHANNEL, buf, &len, READ_TIMEOUT_MS / 2);
            if (ret >= 0 && len > 16)
            {
                got++;
                n_data++;
                last_data_time = time(NULL);
                acc_append(buf, len);
                emit_acc();
            }
        }
        /* 无数据超时：设备停推 90s 则断开重连（run.sh 循环会重新拉起） */
        if (!got && time(NULL) - last_data_time > 90)
        {
            fprintf(stderr, "[recv] no video data for 90s, reconnecting\n");
            break;
        }
        if (!got)
        {
            n_timeout++;
            if (n_timeout % 50 == 0)
                fprintf(stderr, "[recv] poll x%ld (ioctrl=%ld data=%ld err=%ld)\n",
                        n_timeout, n_io, n_data, n_err);
        }
    }
    fprintf(stderr, "[recv] final: timeout=%ld ioctrl=%ld data=%ld err=%ld\n",
            n_timeout, n_io, n_data, n_err);
    free(buf);
    free(iobuf);
}

/* ---- 音频下行接收线程（独立线程，避免阻塞视频主循环） ----
 * 读 ch1: 16B 头 → codec 138/134(G711A) && size<=1024 → 读满 payload → 入环形缓冲 */
static void *audio_recv_thread(void *arg)
{
    int sid = (int)(intptr_t)arg;
    uint8_t hdr[16];
    uint8_t payload[1024];
    long n_frames = 0, n_err = 0;
    fprintf(stderr, "[audio] recv thread start (ch=%d)\n", AUDIO_CHANNEL);
    while (g_running)
    {
        int len = 16;
        int ret = p_Read(sid, (signed char)AUDIO_CHANNEL, hdr, &len, 200);
        if (ret >= 0 && len == 16)
        {
            uint16_t codec = get_le16(hdr);
            uint32_t size = get_le32(hdr + 8);
            if ((codec == 138 || codec == 134) && size > 0 && size <= 1024)
            {
                int plen = 0;
                while (plen < (int)size && g_running)
                {
                    int rl = (int)size - plen;
                    int rr = p_Read(sid, (signed char)AUDIO_CHANNEL,
                                    payload + plen, &rl, 500);
                    if (rr < 0) break;
                    if (rl <= 0) break;
                    plen += rl;
                }
                if (plen == (int)size)
                {
                    audio_enqueue(hdr, payload, plen);
                    n_frames++;
                    if (n_frames % 100 == 0)
                        fprintf(stderr, "[audio] %ld frames enqueued\n", n_frames);
                }
            }
        }
        else if (ret < 0 && ret != -3)
        {
            n_err++;
            if (n_err % 20 == 1)
                fprintf(stderr, "[audio] ch1 read -> %d (err#%ld)\n", ret, n_err);
            if (n_err > 100)
                break;   /* 通道已死，主循环重连后本线程随进程重启 */
        }
    }
    fprintf(stderr, "[audio] recv thread exit (frames=%ld err=%ld)\n", n_frames, n_err);
    return NULL;
}

/* 环形缓冲逐帧发送（客户端专用） */
static void audio_frame_to_fd(int fd, size_t pos)
{
    uint32_t flen = aring_peek_len(pos);
    size_t off = 0;
    size_t total = 4 + flen;
    uint8_t tmp[4096];
    while (off < total && g_running)
    {
        size_t chunk = total - off;
        if (chunk > sizeof(tmp)) chunk = sizeof(tmp);
        for (size_t i = 0; i < chunk; i++)
            tmp[i] = g_aring[(pos + off + i) & ARING_MASK];
        ssize_t w = write(fd, tmp, chunk);
        if (w <= 0) break;
        off += (size_t)w;
    }
}

/* 客户端写线程：从环形缓冲最新帧开始持续推送 */
static void *audio_client_writer(void *arg)
{
    int fd = (int)(intptr_t)arg;
    size_t pos;
    pthread_mutex_lock(&g_aring_mtx);
    pos = g_aring_head;   /* 从最新开始（跳过历史） */
    while (g_running)
    {
        while (pos != g_aring_head)
        {
            uint32_t flen = aring_peek_len(pos);
            /* 帧被覆盖（tail 超过 pos）：跳到最旧有效帧 */
            if (g_aring_tail != g_aring_head)
            {
                size_t dist_tail = (pos >= g_aring_tail) ?
                    pos - g_aring_tail : ARING_SIZE - (g_aring_tail - pos);
                size_t dist_head = (g_aring_head >= pos) ?
                    g_aring_head - pos : ARING_SIZE - (pos - g_aring_head);
                if (dist_head > dist_tail)
                    pos = g_aring_tail;   /* 落后太多，跳到最旧 */
            }
            audio_frame_to_fd(fd, pos);
            pos = (pos + 4 + flen) & ARING_MASK;
        }
        pthread_cond_wait(&g_aring_cond, &g_aring_mtx);
    }
    pthread_mutex_unlock(&g_aring_mtx);
    close(fd);
    return NULL;
}

/* 音频下行服务：监听 127.0.0.1:{g_audio_down_port}，每连接一写线程 */
static void *audio_down_server(void *arg)
{
    (void)arg;
    int lsock = socket(AF_INET, SOCK_STREAM, 0);
    if (lsock < 0)
    {
        fprintf(stderr, "[audio] socket fail\n");
        return NULL;
    }
    int one = 1;
    setsockopt(lsock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)g_audio_down_port);
    if (bind(lsock, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
        fprintf(stderr, "[audio] bind 127.0.0.1:%d fail\n", g_audio_down_port);
        close(lsock);
        return NULL;
    }
    listen(lsock, 4);
    fprintf(stderr, "[audio] down listening 127.0.0.1:%d\n", g_audio_down_port);
    while (g_running)
    {
        int csock = accept(lsock, NULL, NULL);
        if (csock < 0) break;
        pthread_t th;
        if (pthread_create(&th, NULL, audio_client_writer, (void *)(intptr_t)csock) != 0)
        {
            fprintf(stderr, "[audio] client thread create fail\n");
            close(csock);
            continue;
        }
        pthread_detach(th);
        fprintf(stderr, "[audio] down client connected\n");
    }
    close(lsock);
    return NULL;
}

int main(int argc, char *argv[])
{
    if (argc < 4)
    {
        fprintf(stderr,
                "Usage: %s <p2pid> <pwd> <initstring> [mode]\n"
                "  p2pid      来自 blob 解密 (如 TGSH-090469-UBMJM,WWYUYY)\n"
                "  pwd        来自 blob 解密 (≤48 字节)\n"
                "  initstring 来自 blob 解密，去掉 ppcs: 前缀\n"
                "  mode       连接模式，默认 123\n",
                argv[0]);
        return 1;
    }
    const char *p2pid = argv[1];
    const char *pwd = argv[2];
    const char *initstring = argv[3];
    int mode = (argc > 4) ? atoi(argv[4]) : 0;  /* 默认禁 LAN 搜索(走云中继) */
    if (argc > 5)
        g_ctrl_port = atoi(argv[5]);   /* PTZ 控制端口 */
    if (argc > 6)
        g_audio_down_port = atoi(argv[6]); /* 音频下行端口 */

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    setvbuf(stdout, NULL, _IONBF, 0);   /* H264 数据无缓冲直出 */
    setvbuf(stderr, NULL, _IONBF, 0);   /* 日志实时（重定向到文件时避免全缓冲滞后） */

    /* PTZ 控制线程（独立于收流主循环） */
    pthread_t ctrl_th;
    if (g_ctrl_port > 0)
    {
        if (pthread_create(&ctrl_th, NULL, ctrl_server, NULL) != 0)
            fprintf(stderr, "[ctrl] thread create fail\n");
    }

    /* 音频下行线程（双向语音）：独立接收线程 + 下行推送服务 */
    pthread_t audio_recv_th, audio_down_th;
    if (g_audio_down_port > 0)
    {
        if (pthread_create(&audio_down_th, NULL, audio_down_server, NULL) != 0)
            fprintf(stderr, "[audio] down server thread create fail\n");
    }

    if (load_ppcs() < 0)
        return 1;
    fprintf(stderr, "[init] PPCS API version: %d\n", p_GetAPIVersion());

    if (ppcs_init() < 0)
        return 1;

    int sid = ppcs_connect(p2pid, initstring, mode);
    if (sid < 0)
    {
        fprintf(stderr, "[conn] connect failed, cannot continue\n");
        p_DeInitialize();
        return 1;
    }
    g_sid = sid;   /* 供控制线程转发命令 */

    /* PPCS_Check 验证会话（Java startConnect 在 sendPwd 前调用） */
    {
        uint8_t session_buf[256];
        memset(session_buf, 0, sizeof(session_buf));
        int cret = p_Check(sid, session_buf);
        fprintf(stderr, "[conn] PPCS_Check(SID=%d) -> %d\n", sid, cret);
    }

    send_start_stream(sid, pwd);
    /* 音频下行接收线程（需要 sid；独立线程读 ch1 不阻塞视频主循环） */
    if (g_audio_down_port > 0)
    {
        if (pthread_create(&audio_recv_th, NULL, audio_recv_thread,
                           (void *)(intptr_t)sid) != 0)
            fprintf(stderr, "[audio] recv thread create fail\n");
    }
    recv_loop(sid);

    p_Close(sid);
    p_DeInitialize();
    fprintf(stderr, "[init] exit\n");
    return 0;
}

/* ---- PTZ 控制通道（TCP 127.0.0.1:<ctrl_port>） ----
 * 收到 JSON: {"io":4097,"payload":"<hex>"} -> PPCS_Write(sid, ch0, IOCTRL 包)
 * PTZ 例: {"io":4097,"payload":"010000000100"}  = UP, speed=0x64
 *        payload 6B [direction, 0, 0, 0, 0, speed]
 * 方向: 0=STOP 1=UP 2=DOWN 3=LEFT 4=LEFT_UP 5=LEFT_DOWN 6=RIGHT 7=RIGHT_UP 8=RIGHT_DOWN
 */
static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hex_decode(const char *hex, uint8_t *out, int maxlen)
{
    int n = 0;
    while (*hex && n < maxlen)
    {
        int hi = hex_nibble(hex[0]);
        int lo = hex_nibble(hex[1]);
        if (hi < 0 || lo < 0) break;
        out[n++] = (uint8_t)((hi << 4) | lo);
        hex += 2;
    }
    return n;
}

static void *ctrl_server(void *arg)
{
    (void)arg;
    int lsock = socket(AF_INET, SOCK_STREAM, 0);
    if (lsock < 0)
    {
        fprintf(stderr, "[ctrl] socket fail\n");
        return NULL;
    }
    int one = 1;
    setsockopt(lsock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)g_ctrl_port);
    if (bind(lsock, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
        fprintf(stderr, "[ctrl] bind 127.0.0.1:%d fail\n", g_ctrl_port);
        close(lsock);
        return NULL;
    }
    listen(lsock, 4);
    fprintf(stderr, "[ctrl] listening 127.0.0.1:%d\n", g_ctrl_port);
    while (g_running)
    {
        int csock = accept(lsock, NULL, NULL);
        if (csock < 0) break;
        char buf[8192];
        int n = (int)read(csock, buf, sizeof(buf) - 1);
        if (n > 0)
        {
            buf[n] = 0;
            int io = 0;
            const char *p = strstr(buf, "\"io\"");
            if (p)
            {
                const char *colon = strchr(p, ':');
                if (colon) io = atoi(colon + 1);
            }
            const char *q = strstr(buf, "\"payload\"");
            if (q && g_sid >= 0)
            {
                const char *qs = strchr(q, ':');
                const char *qs2 = qs ? strchr(qs, '"') : NULL;
                const char *start = qs2 ? qs2 + 1 : NULL;
                const char *end = start ? strchr(start, '"') : NULL;
                if (start && end)
                {
                    char hexbuf[1024];
                    int hlen = (int)(end - start);
                    if (hlen > (int)sizeof(hexbuf) - 1) hlen = sizeof(hexbuf) - 1;
                    memcpy(hexbuf, start, hlen);
                    hexbuf[hlen] = 0;
                    uint8_t payload[1024];
                    int plen = hex_decode(hexbuf, payload, sizeof(payload));
                    uint8_t pkt[2048];
                    size_t nbytes = build_ioctrl(pkt, (uint32_t)io, payload, (size_t)plen);
                    int ret = p_Write(g_sid, 0, pkt, (int)nbytes);
                    fprintf(stderr, "[ctrl] io=%d payload_len=%d -> %d\n", io, plen, ret);
                    /* 可选 wait=1: 同步等待设备响应（如 32790 日夜查询 -> 32791） */
                    int wait_resp = (strstr(buf, "\"wait\"") != NULL);
                    if (wait_resp && ret >= 0)
                    {
                        uint8_t rbuf[2048];
                        int rlen = 0;
                        uint32_t rio = 0;
                        if (resp_wait(&rio, rbuf, (int)sizeof(rbuf), &rlen, 5000) == 0)
                        {
                            char resp[4500];
                            int rn = snprintf(resp, sizeof(resp),
                                              "{\"ret\":%d,\"resp_io\":%u,\"resp\":\"", ret, rio);
                            static const char hexd[] = "0123456789abcdef";
                            int i;
                            for (i = 0; i < rlen && rn < (int)sizeof(resp) - 8; i++)
                            {
                                resp[rn++] = hexd[(rbuf[i] >> 4) & 0xf];
                                resp[rn++] = hexd[rbuf[i] & 0xf];
                            }
                            resp[rn++] = '"';
                            resp[rn++] = '}';
                            resp[rn++] = '\n';
                            resp[rn] = 0;
                            write(csock, resp, (size_t)rn);
                        }
                        else
                        {
                            write(csock, "{\"ret\":-3,\"resp\":\"timeout\"}\n", 27);
                        }
                    }
                    else
                    {
                        char resp[64];
                        int rn = snprintf(resp, sizeof(resp), "{\"ret\":%d}\n", ret);
                        write(csock, resp, (size_t)rn);
                    }
                }
                else
                {
                    write(csock, "{\"ret\":-99}\n", 12);
                }
            }
            else
            {
                /* 音频上行: {"audio":"<g711a hex>"} -> PPCS_PktSend(sid, ch1, data, len) */
                const char *a = strstr(buf, "\"audio\"");
                if (a && g_sid >= 0 && p_PktSend)
                {
                    const char *as = strchr(a, ':');
                    const char *as2 = as ? strchr(as, '"') : NULL;
                    const char *start = as2 ? as2 + 1 : NULL;
                    const char *end = start ? strchr(start, '"') : NULL;
                    if (start && end)
                    {
                        char hexbuf[8192];
                        int hlen = (int)(end - start);
                        if (hlen > (int)sizeof(hexbuf) - 1) hlen = sizeof(hexbuf) - 1;
                        memcpy(hexbuf, start, hlen);
                        hexbuf[hlen] = 0;
                        uint8_t audio[4096];
                        int alen = hex_decode(hexbuf, audio, sizeof(audio));
                        /* 音频上行: PPCS_Write(sid, ch5, SFrameInfo+G711A) (Cs2Camera.sendAudioData) */
                        int ret = p_Write(g_sid, 5, audio, alen);
                        fprintf(stderr, "[ctrl] audio len=%d -> %d\n", alen, ret);
                        char resp[64];
                        int rn = snprintf(resp, sizeof(resp), "{\"ret\":%d}\n", ret);
                        write(csock, resp, (size_t)rn);
                    }
                    else
                    {
                        write(csock, "{\"ret\":-99}\n", 12);
                    }
                }
                else
                {
                    write(csock, "{\"ret\":-98}\n", 12);
                }
            }
        }
        close(csock);
    }
    close(lsock);
    return NULL;
}
