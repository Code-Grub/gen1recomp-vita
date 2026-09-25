/* HTTPS reachability probe for the PS Vita.
 *
 * The mod index lives on GitHub Pages over HTTPS, and the design at
 * docs/superpowers/specs/2026-09-25-vita-mod-index-network-design.md turns on
 * one unanswered question: can the FIRMWARE's TLS (sceSsl behind sceHttp)
 * negotiate with GitHub at all?  GitHub requires TLS 1.2 with modern cipher
 * suites, and this console's SSL stack is old.
 *
 * This probe answers that before any of the transport is written, and it
 * separates the two failure modes that matter, because they lead to opposite
 * decisions:
 *
 *   - certificate REJECTED but handshake fine  ->  the plan works.  Ship our
 *     own CA roots with sceHttpsLoadCert and relax only the date checks.
 *   - handshake itself fails                   ->  the plan is dead.  Fall back
 *     to vdpm curl + mbedtls, or a plain-HTTP mirror.
 *
 * It does that with a ladder: default verification, then with only the
 * clock-dependent date checks disabled, then with server verification off
 * entirely.  If the last rung still fails, TLS itself is the wall.  A plain
 * HTTP control proves the network path works independently of TLS, so a total
 * failure cannot be misread as "no Wi-Fi".
 *
 * The console's clock is also reported: this device has been off its charger,
 * so a skewed RTC is a live hypothesis for certificate date failures.
 *
 * Read-only.  Writes one report to ux0:data/g1r-net.txt and nothing else.
 */

#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#include <psp2/kernel/processmgr.h>
#include <psp2/sysmodule.h>
#include <psp2/rtc.h>
#include <psp2/net/net.h>
#include <psp2/net/netctl.h>
#include <psp2/net/http.h>

/* This SDK ships libSceSsl_stub.a but no psp2/net/ssl.h, so declare what we
 * use.  Confirmed against the stub's exports. */
int sceSslInit(unsigned int poolSize);
int sceSslTerm(void);

#define NET_POOL_SIZE  (256 * 1024)
#define SSL_POOL_SIZE  (300 * 1024)
#define HTTP_POOL_SIZE (100 * 1024)

/* Both hosts the index resolver can produce (src/mods/ModIndex.lua:69-71).
 * They may present different certificate chains, so both are probed. */
#define URL_PAGES "https://bryanthaboi.github.io/gen1recomp-mod-index/data/index.json"
#define URL_RAW   "https://raw.githubusercontent.com/bryanthaboi/gen1recomp-mod-index/main/site/data/index.json"
#define URL_HTTP  "http://example.com/"

static FILE *log_file;

static void logf_(const char *fmt, ...)
{
	va_list ap;
	if (!log_file)
		return;
	va_start(ap, fmt);
	vfprintf(log_file, fmt, ap);
	va_end(ap);
	fputc('\n', log_file);
	fflush(log_file);
}

static void *net_pool;

/* Returns 0 on success, or the failing sce error. */
static int net_up(void)
{
	SceNetInitParam param;
	int state = -1;
	int ret;

	ret = sceSysmoduleLoadModule(SCE_SYSMODULE_NET);
	logf_("sysmodule NET        : 0x%08X", ret);
	if (ret < 0)
		return ret;

	net_pool = malloc(NET_POOL_SIZE);
	if (!net_pool) {
		logf_("net pool alloc       : FAILED (%d bytes)", NET_POOL_SIZE);
		return -1;
	}
	param.memory = net_pool;
	param.size = NET_POOL_SIZE;
	param.flags = 0;

	ret = sceNetInit(&param);
	/* Already-initialised is not a failure for our purposes. */
	logf_("sceNetInit           : 0x%08X", ret);

	ret = sceNetCtlInit();
	logf_("sceNetCtlInit        : 0x%08X", ret);

	ret = sceNetCtlInetGetState(&state);
	logf_("netctl state         : 0x%08X state=%d (3 == CONNECTED)", ret, state);
	if (state != SCE_NETCTL_STATE_CONNECTED) {
		logf_("  NOT ASSOCIATED. Join Wi-Fi and re-run; every result below is void.");
		return -1;
	}

	ret = sceSysmoduleLoadModule(SCE_SYSMODULE_HTTP);
	logf_("sysmodule HTTP       : 0x%08X", ret);
	ret = sceSysmoduleLoadModule(SCE_SYSMODULE_SSL);
	logf_("sysmodule SSL        : 0x%08X", ret);
	ret = sceSysmoduleLoadModule(SCE_SYSMODULE_HTTPS);
	logf_("sysmodule HTTPS      : 0x%08X", ret);

	ret = sceSslInit(SSL_POOL_SIZE);
	logf_("sceSslInit           : 0x%08X", ret);
	ret = sceHttpInit(HTTP_POOL_SIZE);
	logf_("sceHttpInit          : 0x%08X", ret);

	return 0;
}

static void report_clock(void)
{
	SceDateTime t;
	int ret = sceRtcGetCurrentClockUtc(&t);

	if (ret < 0) {
		logf_("console clock (UTC)  : unavailable 0x%08X", ret);
		return;
	}
	logf_("console clock (UTC)  : %04u-%02u-%02u %02u:%02u:%02u",
	      t.year, t.month, t.day, t.hour, t.minute, t.second);
	logf_("  If this is far off, certificate date checks will fail on a good cert.");
}

/* One GET.  Returns the HTTP status on success, or a negative sce error.
 * Every handle is freed on every path. */
static int attempt(const char *label, const char *url)
{
	int tmpl = -1, conn = -1, req = -1;
	int status = 0, ret;
	int ssl_err = 0;
	unsigned int ssl_detail = 0;
	long long total = 0;
	char buf[4096];

	logf_("");
	logf_("--- %s", label);
	logf_("    %s", url);

	tmpl = sceHttpCreateTemplate("gen1recomp-net-probe", SCE_HTTP_VERSION_1_1, 1);
	if (tmpl < 0) {
		logf_("    createTemplate   : 0x%08X", tmpl);
		return tmpl;
	}
	/* GitHub Pages and raw both redirect; without this a 301 reads as success
	 * with an empty body. */
	ret = sceHttpSetAutoRedirect(tmpl, 1);
	logf_("    autoRedirect     : 0x%08X", ret);

	conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
	if (conn < 0) {
		logf_("    createConnection : 0x%08X", conn);
		sceHttpDeleteTemplate(tmpl);
		return conn;
	}

	req = sceHttpCreateRequestWithURL(conn, SCE_HTTP_METHOD_GET, url, 0);
	if (req < 0) {
		logf_("    createRequest    : 0x%08X", req);
		sceHttpDeleteConnection(conn);
		sceHttpDeleteTemplate(tmpl);
		return req;
	}

	ret = sceHttpSendRequest(req, NULL, 0);
	logf_("    sendRequest      : 0x%08X", ret);
	if (ret < 0) {
		/* This is the interesting branch: ask WHY the TLS layer objected. */
		if (sceHttpsGetSslError(req, &ssl_err, &ssl_detail) >= 0)
			logf_("    sslError         : err=0x%08X detail=0x%08X",
			      ssl_err, ssl_detail);
		else
			logf_("    sslError         : unavailable (not a TLS failure?)");
		goto done;
	}

	ret = sceHttpGetStatusCode(req, &status);
	logf_("    statusCode       : 0x%08X status=%d", ret, status);

	for (;;) {
		int n = sceHttpReadData(req, buf, sizeof(buf));
		if (n < 0) {
			logf_("    readData         : 0x%08X after %lld bytes", n, total);
			break;
		}
		if (n == 0)
			break;
		total += n;
		/* The index is large and the answer does not need all of it. */
		if (total >= 64 * 1024)
			break;
	}
	logf_("    bytes read       : %lld%s", total,
	      total >= 64 * 1024 ? " (stopped early, enough to prove it)" : "");
	if (total > 0)
		logf_("    SUCCESS");

done:
	sceHttpDeleteRequest(req);
	sceHttpDeleteConnection(conn);
	sceHttpDeleteTemplate(tmpl);
	return ret < 0 ? ret : status;
}

int main(int argc, char *argv[])
{
	int ret;

	(void)argc;
	(void)argv;

	log_file = fopen("ux0:data/g1r-net.txt", "w");
	logf_("gen1recomp Vita network probe");
	logf_("=============================");
	logf_("");

	report_clock();
	logf_("");

	if (net_up() != 0) {
		logf_("");
		logf_("network stack did not come up; stopping.");
		goto out;
	}

	/* Control: proves DNS, routing and sockets work with TLS out of the
	 * picture.  If this fails too, the problem is not TLS. */
	attempt("CONTROL: plain HTTP, no TLS at all", URL_HTTP);

	/* Rung 1: exactly what a naive implementation would do. */
	attempt("RUNG 1: HTTPS, firmware default verification", URL_PAGES);

	/* Rung 2: the clock-skew hypothesis.  If this passes where rung 1 failed,
	 * the RTC is the whole problem and the design's date relaxation is right. */
	ret = sceHttpsDisableOption(SCE_HTTPS_FLAG_NOT_AFTER_CHECK
	                            | SCE_HTTPS_FLAG_NOT_BEFORE_CHECK);
	logf_("");
	logf_("disable date checks  : 0x%08X", ret);
	attempt("RUNG 2: HTTPS, date checks disabled", URL_PAGES);

	/* Rung 3: the decisive one.  Passing here means the handshake and ciphers
	 * are fine and only chain validation failed, so shipping our own roots
	 * fixes it.  Failing here means sceSsl cannot talk to GitHub at all. */
	ret = sceHttpsDisableOption(SCE_HTTPS_FLAG_SERVER_VERIFY
	                            | SCE_HTTPS_FLAG_CN_CHECK
	                            | SCE_HTTPS_FLAG_KNOWN_CA_CHECK);
	logf_("");
	logf_("disable verification : 0x%08X", ret);
	attempt("RUNG 3: HTTPS, server verification off (Pages)", URL_PAGES);

	/* Same rung against the other host: a different chain may behave
	 * differently, and the engine can be pointed at either. */
	attempt("RUNG 3b: HTTPS, server verification off (raw)", URL_RAW);

	logf_("");
	logf_("=============================");
	logf_("How to read this:");
	logf_("  CONTROL fails            -> not a TLS problem. Check Wi-Fi/DNS.");
	logf_("  RUNG 1 succeeds          -> nothing to build. Firmware TLS is fine.");
	logf_("  RUNG 2 succeeds, 1 fails -> the clock is the problem. Plan is right.");
	logf_("  RUNG 3 succeeds, 2 fails -> chain validation only. Ship our own roots.");
	logf_("  RUNG 3 fails             -> sceSsl cannot reach GitHub. Plan is dead;");
	logf_("                              fall back to mbedtls or an HTTP mirror.");

out:
	if (log_file) {
		fclose(log_file);
		log_file = NULL;
	}
	sceKernelExitProcess(0);
	return 0;
}
