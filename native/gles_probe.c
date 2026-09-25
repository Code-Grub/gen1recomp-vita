/* Minimal SDL2 + GLES2 probe for the PS Vita.
 *
 * LÖVE fails with eglCreateContext -> EGL_BAD_ALLOC on this console. This asks
 * the same driver, through the same SDL build, with progressively plainer
 * attributes, and writes the outcome of each to ux0:data/g1r-gles.txt. It
 * answers one question: does the PowerVR driver work here at all, and if so
 * which attribute combination does it accept?
 */

#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <SDL2/SDL.h>
#include <GLES2/gl2.h>

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

struct attempt
{
	const char *name;
	int red, green, blue, alpha, depth, stencil, msaa;
};

static const struct attempt ATTEMPTS[] = {
	{ "defaults (nothing set)",      -1, -1, -1, -1, -1, -1, -1 },
	{ "rgb565, no depth/stencil",     5,  6,  5,  0,  0,  0,  0 },
	{ "rgba8888, no depth/stencil",   8,  8,  8,  8,  0,  0,  0 },
	{ "rgba8888, depth24",            8,  8,  8,  8, 24,  0,  0 },
	{ "rgba8888, depth24 stencil8",   8,  8,  8,  8, 24,  8,  0 },
	{ "love-like: rgb8 a8 d16 s8",    8,  8,  8,  8, 16,  8,  0 },
};

static int try_one(const struct attempt *a)
{
	SDL_Window *win;
	SDL_GLContext ctx;

	SDL_GL_ResetAttributes();
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
	if (a->red >= 0) {
		SDL_GL_SetAttribute(SDL_GL_RED_SIZE, a->red);
		SDL_GL_SetAttribute(SDL_GL_GREEN_SIZE, a->green);
		SDL_GL_SetAttribute(SDL_GL_BLUE_SIZE, a->blue);
		SDL_GL_SetAttribute(SDL_GL_ALPHA_SIZE, a->alpha);
		SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, a->depth);
		SDL_GL_SetAttribute(SDL_GL_STENCIL_SIZE, a->stencil);
		SDL_GL_SetAttribute(SDL_GL_MULTISAMPLEBUFFERS, a->msaa > 0 ? 1 : 0);
		SDL_GL_SetAttribute(SDL_GL_MULTISAMPLESAMPLES, a->msaa > 0 ? a->msaa : 0);
	}

	win = SDL_CreateWindow("gles probe", SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
			960, 544, SDL_WINDOW_OPENGL);
	if (!win) {
		logf_("%-30s SDL_CreateWindow failed: %s", a->name, SDL_GetError());
		return 0;
	}

	ctx = SDL_GL_CreateContext(win);
	if (!ctx) {
		logf_("%-30s context failed: %s", a->name, SDL_GetError());
		SDL_DestroyWindow(win);
		return 0;
	}

	logf_("%-30s CONTEXT OK | vendor=%s renderer=%s version=%s", a->name,
		(const char *) glGetString(GL_VENDOR), (const char *) glGetString(GL_RENDERER),
		(const char *) glGetString(GL_VERSION));

	/* prove it can actually draw and flip */
	for (int i = 0; i < 60; i++) {
		glClearColor(0.1f, 0.5f, 0.2f, 1.0f);
		glClear(GL_COLOR_BUFFER_BIT);
		SDL_GL_SwapWindow(win);
	}
	logf_("%-30s 60 frames presented", a->name);

	SDL_GL_DeleteContext(ctx);
	SDL_DestroyWindow(win);
	return 1;
}

int main(int argc, char *argv[])
{
	(void) argc;
	(void) argv;

	log_file = fopen("ux0:data/g1r-gles.txt", "w");
	logf_("SDL %d.%d.%d, minimal GLES2 probe", SDL_MAJOR_VERSION, SDL_MINOR_VERSION, SDL_PATCHLEVEL);

	if (SDL_Init(SDL_INIT_VIDEO) != 0) {
		logf_("SDL_Init(VIDEO) failed: %s", SDL_GetError());
		if (log_file) fclose(log_file);
		return 1;
	}
	logf_("SDL_Init ok, video driver = %s", SDL_GetCurrentVideoDriver());

	for (size_t i = 0; i < sizeof(ATTEMPTS) / sizeof(ATTEMPTS[0]); i++) {
		if (try_one(&ATTEMPTS[i]))
			logf_("--> first working combination: %s", ATTEMPTS[i].name);
	}

	logf_("done");
	if (log_file)
		fclose(log_file);
	SDL_Quit();
	return 0;
}
