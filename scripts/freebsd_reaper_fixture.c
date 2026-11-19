/* Experimental lifecycle fixture only. Not a resource or security sandbox. */
#include <sys/types.h>
#include <sys/procctl.h>
#include <sys/wait.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;
static void on_signal(int sig) { interrupted = sig; }
static long long millis(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) { perror("clock_gettime"); exit(125); }
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void pause_tick(void) { struct timespec t = {0, 10000000}; nanosleep(&t, NULL); }
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: reaper TIMEOUT_MS COMMAND [ARGS]\n"); return 125; }
    char *end;
    errno = 0;
    long budget = strtol(argv[1], &end, 10);
    if (errno || *end || budget < 50 || budget > 5000) return 125;
    /* Acquire before creating any child; only this invocation's descendants belong here. */
    if (procctl(P_PID, getpid(), PROC_REAP_ACQUIRE, NULL) != 0) { perror("acquire"); return 125; }
    struct sigaction sa = {0};
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGTERM, &sa, NULL) || sigaction(SIGINT, &sa, NULL)) return 125;
    pid_t child = fork();
    if (child < 0) { perror("fork"); return 125; }
    if (child == 0) {
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL);
        execvp(argv[2], argv + 2);
        perror("execvp"); _exit(127);
    }
    long long deadline = millis() + budget;
    int status = 0, result = 125;
    for (;;) {
        pid_t got = waitpid(child, &status, WNOHANG);
        if (got == child) {
            result = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
            break;
        }
        if (got < 0 && errno != EINTR) { perror("wait child"); break; }
        if (interrupted) { result = 128 + interrupted; break; }
        if (millis() >= deadline) { result = 124; break; }
        pause_tick();
    }
    /* Always clean up, including a successful direct child that abandoned descendants. */
    long long cleanup_deadline = millis() + 2000;
    for (;;) {
        struct procctl_reaper_kill kill_request = {0};
        kill_request.rk_sig = SIGKILL;
        kill_request.rk_fpid = -1;
        if (procctl(P_PID, getpid(), PROC_REAP_KILL, &kill_request) != 0 && errno != ESRCH) {
            perror("reap kill"); return 125;
        }
        if (kill_request.rk_fpid != -1) { fprintf(stderr, "descendant signal failure\n"); return 125; }
        pid_t got;
        while ((got = waitpid(-1, &status, WNOHANG)) > 0) {}
        if (got == -1 && errno == ECHILD) return result;
        if (got == -1 && errno != EINTR) { perror("reap wait"); return 125; }
        if (millis() >= cleanup_deadline) { fprintf(stderr, "cleanup deadline exceeded\n"); return 125; }
        pause_tick();
    }
}
