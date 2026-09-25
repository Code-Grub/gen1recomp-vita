/* HTTPS reachability probe for the PS Vita.
 *
 * Supports the design at
 * docs/superpowers/specs/2026-09-25-vita-mod-index-network-design.md.
 *
 * RUN 1 (evidence/2026-09-25-g1r-net-probe-run1.txt): stack up, netctl
 * CONNECTED, DNS and TCP to GitHub fine, TLS handshake fails with
 * HTTPS_ERROR_HANDSHAKE and an EMPTY detail mask -- no certificate complaint.
 *
 * RUN 2 (evidence/2026-09-25-g1r-net-probe-run2.txt): HTTPS WORKS here.
 * TLS 1.0, 1.1, 1.2 and badssl.com's ordinary modern certificate all returned
 * 200, so sceSsl negotiates TLS 1.2 and validates a real chain. GitHub still
 * failed identically on pools 7x and 20x larger, so pool size is ruled out.
 * Also found: SERVER_VERIFY, CN_CHECK and KNOWN_CA_CHECK CANNOT be disabled on
 * this firmware (0x8043506B), so certificate verification is permanently on.
 *
 * RUN 3, this one, answers the single question left: how narrow is the GitHub
 * failure?  badssl.com deliberately accepts weak and old configurations, so
 * passing it proved less than it looked.  GitHub Pages requires modern ECDHE
 * key exchange, AEAD cipher suites, and SNI.  This run asks which of those is
 * missing, by testing hosts that need each one.
 *
 * The decision it drives, fixed in advance:
 *   - Cloudflare and Google pass -> mirror the index behind a permissive host.
 *     No new TLS code; the bridge alone is enough.
 *   - only old/weak badssl passes -> the transport must bring its own TLS
 *     (vdpm curl + mbedtls), the largest option on the table.
 *
 * SNI is covered by inference rather than a dedicated test: Cloudflare and
 * Google are both massively multi-tenant and cannot serve a correct certificate
 * without SNI, so either passing proves sceHttp sends it.
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

int sceSslInit(unsigned int poolSize);
int sceSslTerm(void);

#define NET_POOL_SIZE  (256 * 1024)
/* Run 2 proved these are ample and that size is not the variable. */
#define SSL_POOL_SIZE  (2 * 1024 * 1024)
#define HTTP_POOL_SIZE (2 * 1024 * 1024)

static FILE *log_file;
static void *net_pool;

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

static const char *err_name(int e)
{
	switch ((unsigned int)e) {
	case 0x80431075u: return "HTTP_ERROR_SSL";
	case 0x80436007u: return "RESOLVER_ENOHOST";
	case 0x80436009u: return "RESOLVER_ESERVERREFUSED";
	case 0x8043600Au: return "RESOLVER_ENORECORD";
	case 0x80431063u: return "HTTP_ERROR_NETWORK";
	case 0x80431068u: return "HTTP_ERROR_TIMEOUT";
	case 0x80431022u: return "HTTP_ERROR_OUT_OF_MEMORY";
	case 0x80435060u: return "HTTPS_ERROR_CERT";
	case 0x80435061u: return "HTTPS_ERROR_HANDSHAKE";
	case 0x80435062u: return "HTTPS_ERROR_IO";
	case 0x80435063u: return "HTTPS_ERROR_INTERNAL";
	default: return "";
	}
}

static void detail_bits(unsigned int d, char *out, int n)
{
	out[0] = 0;
	if (d == 0) {
		snprintf(out, n, "none");
		return;
	}
	if (d & SCE_HTTPS_ERROR_SSL_INTERNAL)         strncat(out, "INTERNAL ", n - strlen(out) - 1);
	if (d & SCE_HTTPS_ERROR_SSL_INVALID_CERT)     strncat(out, "INVALID_CERT ", n - strlen(out) - 1);
	if (d & SCE_HTTPS_ERROR_SSL_CN_CHECK)         strncat(out, "CN_CHECK ", n - strlen(out) - 1);
	if (d & SCE_HTTPS_ERROR_SSL_NOT_AFTER_CHECK)  strncat(out, "NOT_AFTER ", n - strlen(out) - 1);
	if (d & SCE_HTTPS_ERROR_SSL_NOT_BEFORE_CHECK) strncat(out, "NOT_BEFORE ", n - strlen(out) - 1);
	if (d & SCE_HTTPS_ERROR_SSL_UNKNOWN_CA)       strncat(out, "UNKNOWN_CA ", n - strlen(out) - 1);
}

/* One GET.  Logs a single line per host plus detail when it fails.
 * A CERT error is reported as HANDSHAKE-OK, because reaching certificate
 * evaluation means negotiation already succeeded -- that distinction is the
 * whole point of this probe. */
static void attempt(const char *label, const char *url)
{
	int tmpl, conn, req, status = 0, ret;
	int ssl_err = 0;
	unsigned int ssl_detail = 0;
	long long total = 0;
	char buf[2048];
	char bits[128];

	tmpl = sceHttpCreateTemplate("gen1recomp-net-probe", SCE_HTTP_VERSION_1_1, 1);
	if (tmpl < 0) {
		logf_("  %-26s TEMPLATE FAIL 0x%08X", label, tmpl);
		return;
	}
	sceHttpSetAutoRedirect(tmpl, 1);

	conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
	if (conn < 0) {
		logf_("  %-26s CONNECT FAIL  0x%08X %s", label, conn, err_name(conn));
		sceHttpDeleteTemplate(tmpl);
		return;
	}
	req = sceHttpCreateRequestWithURL(conn, SCE_HTTP_METHOD_GET, url, 0);
	if (req < 0) {
		logf_("  %-26s REQUEST FAIL  0x%08X %s", label, req, err_name(req));
		sceHttpDeleteConnection(conn);
		sceHttpDeleteTemplate(tmpl);
		return;
	}

	ret = sceHttpSendRequest(req, NULL, 0);
	if (ret < 0) {
		/* Always report the sendRequest error. Run 3 logged ssl_err alone here,
		 * so a NON-TLS failure printed as 0x00000000 and its real cause was
		 * lost (cloudflare.com, run 3). Never discard `ret` again. */
		if (sceHttpsGetSslError(req, &ssl_err, &ssl_detail) >= 0 && ssl_err != 0) {
			detail_bits(ssl_detail, bits, sizeof(bits));
			if ((unsigned int)ssl_err == 0x80435060u)
				logf_("  %-26s HANDSHAKE OK, cert rejected [%s] (send 0x%08X)",
				      label, bits, ret);
			else
				logf_("  %-26s FAIL send=0x%08X %s ssl=0x%08X %s detail=[%s]",
				      label, ret, err_name(ret), ssl_err, err_name(ssl_err), bits);
		} else {
			logf_("  %-26s FAIL send=0x%08X %s (no TLS error reported)",
			      label, ret, err_name(ret));
		}
		goto done;
	}

	sceHttpGetStatusCode(req, &status);
	for (;;) {
		int n = sceHttpReadData(req, buf, sizeof(buf));
		if (n <= 0)
			break;
		total += n;
		if (total >= 8 * 1024)
			break;
	}
	logf_("  %-26s OK status=%d bytes=%lld", label, status, total);

done:
	sceHttpDeleteRequest(req);
	sceHttpDeleteConnection(conn);
	sceHttpDeleteTemplate(tmpl);
}

struct host { const char *label; const char *url; };

/* The decisive group: multi-tenant, modern-cipher, SNI-requiring hosts that
 * are not GitHub.  If these pass, a mirror is the whole fallback. */
static const struct host MAINSTREAM[] = {
	{ "cloudflare.com",        "https://www.cloudflare.com/" },
	{ "google.com",            "https://www.google.com/" },
	{ "api.github.com",        "https://api.github.com/" },
	{ "objects.githubusercontent", "https://objects.githubusercontent.com/" },
	{ "codeload.github.com",   "https://codeload.github.com/" },
};

/* The known failure, repeated so this run is self-contained. */
static const struct host CONTROL[] = {
	{ "github.io (known fail)", "https://bryanthaboi.github.io/gen1recomp-mod-index/data/index.json" },
	{ "badssl.com (known ok)",  "https://badssl.com/" },
};

/* Names the missing capability instead of guessing it. */
static const struct host CIPHERS[] = {
	{ "ecc256 (P-256 ECDSA)",  "https://ecc256.badssl.com/" },
	{ "ecc384 (P-384 ECDSA)",  "https://ecc384.badssl.com/" },
	{ "rsa2048",               "https://rsa2048.badssl.com/" },
	{ "rsa4096",               "https://rsa4096.badssl.com/" },
	{ "sha384 sig",            "https://sha384.badssl.com/" },
	{ "sha512 sig",            "https://sha512.badssl.com/" },
	{ "cbc suite (old)",       "https://cbc.badssl.com/" },
	{ "3des suite (ancient)",  "https://3des.badssl.com/" },
};

/* Sanity check that our detail decoding is real: this SHOULD report a cert
 * rejection with CN_CHECK, which also proves cert evaluation is reached. */
static const struct host SANITY[] = {
	{ "mismatch cert",         "https://mismatch.badssl.com/" },
	{ "expired cert",          "https://expired.badssl.com/" },
};

static void run(const char *title, const struct host *hosts, int n)
{
	int i;
	logf_("");
	logf_("=== %s", title);
	for (i = 0; i < n; i++)
		attempt(hosts[i].label, hosts[i].url);
}

static int net_up(void)
{
	SceNetInitParam param;
	int state = -1;

	if (sceSysmoduleLoadModule(SCE_SYSMODULE_NET) < 0)
		return -1;
	net_pool = malloc(NET_POOL_SIZE);
	if (!net_pool)
		return -1;
	param.memory = net_pool;
	param.size = NET_POOL_SIZE;
	param.flags = 0;
	sceNetInit(&param);
	sceNetCtlInit();
	sceNetCtlInetGetState(&state);
	logf_("netctl state         : %d (3 == CONNECTED)", state);
	if (state != SCE_NETCTL_STATE_CONNECTED) {
		logf_("NOT ASSOCIATED. Join Wi-Fi and re-run.");
		return -1;
	}
	sceSysmoduleLoadModule(SCE_SYSMODULE_HTTP);
	sceSysmoduleLoadModule(SCE_SYSMODULE_SSL);
	sceSysmoduleLoadModule(SCE_SYSMODULE_HTTPS);
	logf_("sceSslInit           : 0x%08X", sceSslInit(SSL_POOL_SIZE));
	logf_("sceHttpInit          : 0x%08X", sceHttpInit(HTTP_POOL_SIZE));
	return 0;
}

int main(int argc, char *argv[])
{
	SceDateTime t;

	(void)argc;
	(void)argv;

	log_file = fopen("ux0:data/g1r-net.txt", "w");
	logf_("gen1recomp Vita network probe -- RUN 3");
	logf_("======================================");
	logf_("Run 2 proved HTTPS works here (TLS 1.2 and a real chain both fine).");
	logf_("This run asks how narrow the GitHub failure is, and therefore which");
	logf_("fallback to build. 'HANDSHAKE OK, cert rejected' is a PASS for the");
	logf_("negotiation question: it means TLS itself completed.");
	logf_("");

	if (sceRtcGetCurrentClockUtc(&t) >= 0)
		logf_("clock (UTC)          : %04u-%02u-%02u %02u:%02u:%02u",
		      t.year, t.month, t.day, t.hour, t.minute, t.second);

	if (net_up() != 0)
		goto out;

	run("CONTROLS (one known fail, one known pass)", CONTROL,
	    sizeof(CONTROL) / sizeof(CONTROL[0]));
	run("MAINSTREAM MODERN HOSTS -- the decisive group", MAINSTREAM,
	    sizeof(MAINSTREAM) / sizeof(MAINSTREAM[0]));
	run("CIPHER AND SIGNATURE CAPABILITY", CIPHERS,
	    sizeof(CIPHERS) / sizeof(CIPHERS[0]));
	run("DECODER SANITY (both SHOULD be cert rejections)", SANITY,
	    sizeof(SANITY) / sizeof(SANITY[0]));

	logf_("");
	logf_("======================================");
	logf_("How to read this:");
	logf_("  cloudflare + google OK    -> SNI and modern suites are fine, the");
	logf_("                               failure is GitHub-specific. Mirror the");
	logf_("                               index behind a permissive host; no new");
	logf_("                               TLS code needed.");
	logf_("  they fail, old badssl OK  -> this stack cannot do modern suites.");
	logf_("                               Transport must ship its own TLS");
	logf_("                               (curl + mbedtls).");
	logf_("  ecc* fail, rsa* pass      -> missing ECDHE/ECDSA. That alone");
	logf_("                               explains GitHub and names the gap.");
	logf_("  SANITY rows not rejected  -> the decoder is lying; distrust the");
	logf_("                               detail masks in every run above.");

out:
	if (log_file) {
		fclose(log_file);
		log_file = NULL;
	}
	sceKernelExitProcess(0);
	return 0;
}
