/* HTTPS reachability probe for the PS Vita.
 *
 * The mod index lives on GitHub Pages over HTTPS, and the design at
 * docs/superpowers/specs/2026-09-25-vita-mod-index-network-design.md turns on
 * one unanswered question: can the FIRMWARE's TLS (sceSsl behind sceHttp)
 * negotiate with GitHub at all?  GitHub requires TLS 1.2 with modern cipher
 * suites, and this console's SSL stack is old.
 *
 * RUN 1 (2026-09-25, evidence/2026-09-25-g1r-net-probe-run1.txt) found: stack
 * up, netctl CONNECTED, DNS and TCP to GitHub fine, but the TLS handshake fails
 * with SCE_HTTPS_ERROR_HANDSHAKE and an EMPTY detail mask -- no certificate
 * complaint of any kind.  That killed the "ship our own CA roots" plan, since a
 * stale CA store would report CERT plus UNKNOWN_CA.  It left three loose ends,
 * which is what RUN 2 exists to close:
 *
 *   1. sceHttpsDisableOption(SERVER_VERIFY|CN_CHECK|KNOWN_CA_CHECK) was
 *      REJECTED (0x8043506B), so "verification off" was never actually tested.
 *      Here each flag is disabled individually and the return logged, then the
 *      request is retried with whatever actually took.
 *   2. A HANDSHAKE failure can also come from an undersized SSL or HTTP pool.
 *      So the whole GitHub test runs twice, once on run 1's pool sizes and once
 *      on pools an order of magnitude larger, with sceHttpTerm/sceSslTerm in
 *      between.  Same failure on both sizes rules the pool out.
 *   3. THE DISCRIMINATOR: endpoints that serve deliberately OLD TLS.  If an old
 *      TLS endpoint handshakes while GitHub does not, firmware sceSsl works and
 *      is merely too old for GitHub, which is conclusive and means the fallback
 *      must supply its own TLS (mbedtls).  If nothing handshakes anywhere,
 *      sceHttps is unusable here for some other reason and the fallback has to
 *      replace something different.
 *      Note a CERT error from these hosts is a PASS for this purpose: reaching
 *      certificate evaluation means the handshake itself succeeded.
 *
 * Run 1's plain-HTTP control failed with RESOLVER_ENOHOST against example.com
 * while the HTTPS hosts plainly resolved, so it was anomalous rather than
 * informative.  Here the control uses several hosts and runs at the END, after
 * the resolver has had traffic, to settle whether that was transient.
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

#define NET_POOL_SIZE (256 * 1024)

/* Run 1's sizes, then deliberately generous ones. */
#define SSL_POOL_SMALL  (300 * 1024)
#define HTTP_POOL_SMALL (100 * 1024)
#define SSL_POOL_LARGE  (2 * 1024 * 1024)
#define HTTP_POOL_LARGE (2 * 1024 * 1024)

/* Both hosts the index resolver can produce (src/mods/ModIndex.lua:69-71). */
#define URL_PAGES "https://bryanthaboi.github.io/gen1recomp-mod-index/data/index.json"
#define URL_RAW   "https://raw.githubusercontent.com/bryanthaboi/gen1recomp-mod-index/main/site/data/index.json"

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

static const char *ssl_detail_name(unsigned int d)
{
	if (d == 0)
		return "none set (no certificate complaint at all)";
	if (d & SCE_HTTPS_ERROR_SSL_UNKNOWN_CA)
		return "UNKNOWN_CA (stale CA store: own roots would fix)";
	if (d & SCE_HTTPS_ERROR_SSL_INVALID_CERT)
		return "INVALID_CERT";
	if (d & SCE_HTTPS_ERROR_SSL_CN_CHECK)
		return "CN_CHECK";
	if (d & SCE_HTTPS_ERROR_SSL_NOT_AFTER_CHECK)
		return "NOT_AFTER (clock or expired)";
	if (d & SCE_HTTPS_ERROR_SSL_NOT_BEFORE_CHECK)
		return "NOT_BEFORE (clock)";
	if (d & SCE_HTTPS_ERROR_SSL_INTERNAL)
		return "SSL_INTERNAL";
	return "unrecognised";
}

/* Names the error so the report reads without the SDK headers to hand. */
static const char *err_name(int e)
{
	switch ((unsigned int)e) {
	case 0x80431075u: return "SCE_HTTP_ERROR_SSL";
	case 0x80436007u: return "RESOLVER_ENOHOST";
	case 0x80436009u: return "RESOLVER_ESERVERREFUSED";
	case 0x8043600Au: return "RESOLVER_ENORECORD";
	case 0x80431063u: return "HTTP_ERROR_NETWORK";
	case 0x80431068u: return "HTTP_ERROR_TIMEOUT";
	case 0x80431022u: return "HTTP_ERROR_OUT_OF_MEMORY";
	case 0x80431001u: return "HTTP_ERROR_BEFORE_INIT";
	case 0x80431020u: return "HTTP_ERROR_ALREADY_INITED";
	case 0x80435060u: return "HTTPS_ERROR_CERT";
	case 0x80435061u: return "HTTPS_ERROR_HANDSHAKE";
	case 0x80435062u: return "HTTPS_ERROR_IO";
	case 0x80435063u: return "HTTPS_ERROR_INTERNAL";
	case 0x804311FEu: return "HTTP_ERROR_INVALID_VALUE";
	default: return "";
	}
}

static int net_up_once(void)
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
		logf_("net pool alloc       : FAILED");
		return -1;
	}
	param.memory = net_pool;
	param.size = NET_POOL_SIZE;
	param.flags = 0;
	ret = sceNetInit(&param);
	logf_("sceNetInit           : 0x%08X", ret);

	ret = sceNetCtlInit();
	logf_("sceNetCtlInit        : 0x%08X", ret);
	ret = sceNetCtlInetGetState(&state);
	logf_("netctl state         : 0x%08X state=%d (3 == CONNECTED)", ret, state);
	if (state != SCE_NETCTL_STATE_CONNECTED) {
		logf_("  NOT ASSOCIATED. Join Wi-Fi and re-run; everything below is void.");
		return -1;
	}

	logf_("sysmodule HTTP       : 0x%08X", sceSysmoduleLoadModule(SCE_SYSMODULE_HTTP));
	logf_("sysmodule SSL        : 0x%08X", sceSysmoduleLoadModule(SCE_SYSMODULE_SSL));
	logf_("sysmodule HTTPS      : 0x%08X", sceSysmoduleLoadModule(SCE_SYSMODULE_HTTPS));
	return 0;
}

static void http_up(unsigned int sslPool, unsigned int httpPool)
{
	int ret = sceSslInit(sslPool);
	logf_("sceSslInit(%u KB)%*s: 0x%08X %s", sslPool / 1024,
	      sslPool / 1024 >= 1000 ? 4 : 5, "", ret, err_name(ret));
	ret = sceHttpInit(httpPool);
	logf_("sceHttpInit(%u KB)%*s: 0x%08X %s", httpPool / 1024,
	      httpPool / 1024 >= 1000 ? 3 : 4, "", ret, err_name(ret));
}

static void http_down(void)
{
	int a = sceHttpTerm();
	int b = sceSslTerm();
	logf_("sceHttpTerm / sceSslTerm : 0x%08X / 0x%08X", a, b);
}

/* One GET.  Returns the HTTP status on success, or a negative sce error. */
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
		logf_("    createTemplate   : 0x%08X %s", tmpl, err_name(tmpl));
		return tmpl;
	}
	sceHttpSetAutoRedirect(tmpl, 1);

	conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
	if (conn < 0) {
		logf_("    createConnection : 0x%08X %s", conn, err_name(conn));
		sceHttpDeleteTemplate(tmpl);
		return conn;
	}
	req = sceHttpCreateRequestWithURL(conn, SCE_HTTP_METHOD_GET, url, 0);
	if (req < 0) {
		logf_("    createRequest    : 0x%08X %s", req, err_name(req));
		sceHttpDeleteConnection(conn);
		sceHttpDeleteTemplate(tmpl);
		return req;
	}

	ret = sceHttpSendRequest(req, NULL, 0);
	logf_("    sendRequest      : 0x%08X %s", ret, err_name(ret));
	if (ret < 0) {
		if (sceHttpsGetSslError(req, &ssl_err, &ssl_detail) >= 0) {
			logf_("    sslError         : 0x%08X %s", ssl_err, err_name(ssl_err));
			logf_("    sslDetail        : 0x%08X %s", ssl_detail,
			      ssl_detail_name(ssl_detail));
			if ((unsigned int)ssl_err == 0x80435060u)
				logf_("    NOTE: a CERT error means the HANDSHAKE SUCCEEDED.");
		} else {
			logf_("    sslError         : unavailable (not a TLS failure)");
		}
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
		if (total >= 32 * 1024)
			break;
	}
	logf_("    bytes read       : %lld", total);
	if (total > 0)
		logf_("    SUCCESS");

done:
	sceHttpDeleteRequest(req);
	sceHttpDeleteConnection(conn);
	sceHttpDeleteTemplate(tmpl);
	return ret < 0 ? ret : status;
}

struct flag { unsigned int bit; const char *name; };

static const struct flag FLAGS[] = {
	{ SCE_HTTPS_FLAG_SERVER_VERIFY,   "SERVER_VERIFY"   },
	{ SCE_HTTPS_FLAG_CLIENT_VERIFY,   "CLIENT_VERIFY"   },
	{ SCE_HTTPS_FLAG_CN_CHECK,        "CN_CHECK"        },
	{ SCE_HTTPS_FLAG_NOT_AFTER_CHECK, "NOT_AFTER_CHECK" },
	{ SCE_HTTPS_FLAG_NOT_BEFORE_CHECK,"NOT_BEFORE_CHECK"},
	{ SCE_HTTPS_FLAG_KNOWN_CA_CHECK,  "KNOWN_CA_CHECK"  },
};

/* Closes run 1's caveat 1: which flags can actually be disabled? */
static void disable_flags_individually(void)
{
	unsigned int i;

	logf_("");
	logf_("--- flag-by-flag sceHttpsDisableOption (run 1 rejected the combined mask)");
	for (i = 0; i < sizeof(FLAGS) / sizeof(FLAGS[0]); i++) {
		int ret = sceHttpsDisableOption(FLAGS[i].bit);
		logf_("    %-17s: 0x%08X %s", FLAGS[i].name, ret,
		      ret == 0 ? "accepted" : "REJECTED");
	}
}

int main(int argc, char *argv[])
{
	SceDateTime t;
	int ret;

	(void)argc;
	(void)argv;

	log_file = fopen("ux0:data/g1r-net.txt", "w");
	logf_("gen1recomp Vita network probe -- RUN 2");
	logf_("======================================");
	logf_("Closing run 1's loose ends: pool size, flag rejection, and the");
	logf_("old-TLS discriminator. See native/net_probe.c for what each proves.");
	logf_("");

	if (sceRtcGetCurrentClockUtc(&t) >= 0)
		logf_("console clock (UTC)  : %04u-%02u-%02u %02u:%02u:%02u",
		      t.year, t.month, t.day, t.hour, t.minute, t.second);
	logf_("");

	if (net_up_once() != 0) {
		logf_("network stack did not come up; stopping.");
		goto out;
	}

	/* ---- Phase 1: run 1's pool sizes, to reproduce the baseline. ---- */
	logf_("");
	logf_("=== PHASE 1: run 1's pool sizes (reproduce the baseline)");
	http_up(SSL_POOL_SMALL, HTTP_POOL_SMALL);
	attempt("P1: GitHub Pages, small pools", URL_PAGES);

	/* ---- Phase 2: same request, much larger pools. Single variable. ---- */
	logf_("");
	logf_("=== PHASE 2: same request, pools ~7x and ~20x larger");
	logf_("    A different result here means run 1 was a pool problem, not TLS.");
	http_down();
	http_up(SSL_POOL_LARGE, HTTP_POOL_LARGE);
	attempt("P2: GitHub Pages, large pools", URL_PAGES);
	attempt("P2: GitHub raw, large pools", URL_RAW);

	/* ---- Phase 3: what can actually be disabled, then verify-off retry. ---- */
	logf_("");
	logf_("=== PHASE 3: verification genuinely off (run 1 could not do this)");
	disable_flags_individually();
	attempt("P3: GitHub Pages, whatever disabling took", URL_PAGES);

	/* ---- Phase 4: THE DISCRIMINATOR. Old TLS versus modern TLS. ---- */
	logf_("");
	logf_("=== PHASE 4: old-TLS endpoints -- the decisive comparison");
	logf_("    Handshake here but not at GitHub => sceSsl works, is too old.");
	logf_("    A CERT error counts as a handshake SUCCESS for this question.");
	attempt("P4: TLS 1.0 endpoint", "https://tls-v1-0.badssl.com:1010/");
	attempt("P4: TLS 1.1 endpoint", "https://tls-v1-1.badssl.com:1011/");
	attempt("P4: TLS 1.2 endpoint", "https://tls-v1-2.badssl.com:1012/");
	attempt("P4: plain badssl root", "https://badssl.com/");

	/* ---- Phase 5: the HTTP control, now at the END and on several hosts. ---- */
	logf_("");
	logf_("=== PHASE 5: plain-HTTP control, several hosts, after the TLS traffic");
	logf_("    Run 1's single control failed with RESOLVER_ENOHOST while the");
	logf_("    HTTPS hosts resolved fine, so this settles whether that was real.");
	attempt("P5: Mozilla captive-portal check", "http://detectportal.firefox.com/success.txt");
	attempt("P5: neverssl", "http://neverssl.com/");
	attempt("P5: example.com (run 1's host)", "http://example.com/");

	logf_("");
	logf_("======================================");
	logf_("How to read this:");
	logf_("  P2 succeeds where P1 failed   -> pool size was the bug. Plan revives.");
	logf_("  P3 succeeds where P2 failed   -> cert validation only. Own roots work.");
	logf_("  P4 handshakes, GitHub did not -> CONFIRMED: sceSsl too old for GitHub.");
	logf_("                                   Fallback must bring its own TLS.");
	logf_("  P4 fails everywhere too       -> sceHttps unusable here for another");
	logf_("                                   reason; re-diagnose before choosing.");
	logf_("  P5 all fail                   -> resolver really is broken; that is a");
	logf_("                                   different bug and outranks the rest.");

out:
	if (log_file) {
		fclose(log_file);
		log_file = NULL;
	}
	sceKernelExitProcess(0);
	return 0;
}
