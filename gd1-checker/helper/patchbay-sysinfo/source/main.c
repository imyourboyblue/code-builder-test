// Patchbay helper: writes the console's exact firmware and Atmosphere versions to
// sdmc:/switch/patchbay-sysinfo/sysinfo.txt so Patchbay can read them over FTP.
// It only reads system information. Open it once from the Homebrew Menu.
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <switch.h>

#define FOLDER "sdmc:/switch/patchbay-sysinfo"
#define OUTFILE FOLDER "/sysinfo.txt"

int main(int argc, char **argv) {
    consoleInit(NULL);
    padConfigureInput(1, HidNpadStyleSet_NpadStandard);
    PadState pad;
    padInitializeDefault(&pad);

    char fw[32] = "", display[32] = "", ams[32] = "", commit[32] = "", mmc[16] = "unknown", when[32] = "";

    if (R_SUCCEEDED(setsysInitialize())) {
        SetSysFirmwareVersion v;
        if (R_SUCCEEDED(setsysGetFirmwareVersion(&v))) {
            snprintf(fw, sizeof fw, "%u.%u.%u", (unsigned)v.major, (unsigned)v.minor, (unsigned)v.micro);
            snprintf(display, sizeof display, "%.24s", v.display_version);
        }
        setsysExit();
    }
    if (!fw[0]) {
        u32 h = hosversionGet();
        snprintf(fw, sizeof fw, "%u.%u.%u", (unsigned)HOSVER_MAJOR(h), (unsigned)HOSVER_MINOR(h), (unsigned)HOSVER_MICRO(h));
    }

    if (R_SUCCEEDED(splInitialize())) {
        u64 api = 0, hash = 0, emu = 0;
        // Atmosphere's own config items: 65000 = API version, 65003 = git commit, 65007 = emuMMC type.
        if (R_SUCCEEDED(splGetConfig((SplConfigItem)65000, &api)))
            snprintf(ams, sizeof ams, "%u.%u.%u", (unsigned)((api >> 56) & 0xff), (unsigned)((api >> 48) & 0xff),
                     (unsigned)((api >> 40) & 0xff));
        if (R_SUCCEEDED(splGetConfig((SplConfigItem)65003, &hash)))
            snprintf(commit, sizeof commit, "%08llx", (unsigned long long)(hash & 0xffffffffULL));
        if (R_SUCCEEDED(splGetConfig((SplConfigItem)65007, &emu)))
            snprintf(mmc, sizeof mmc, "%s", emu ? "emuMMC" : "sysMMC");
        splExit();
    }

    time_t now = time(NULL);
    struct tm *t = localtime(&now);
    if (t) strftime(when, sizeof when, "%Y-%m-%d %H:%M:%S", t);

    mkdir(FOLDER, 0777);
    FILE *f = fopen(OUTFILE, "w");
    int ok = 0;
    if (f) {
        fprintf(f, "firmware=%s\n", fw);
        fprintf(f, "firmware_display=%s\n", display);
        fprintf(f, "atmosphere=%s\n", ams[0] ? ams : "");
        fprintf(f, "atmosphere_commit=%s\n", commit);
        fprintf(f, "emummc=%s\n", mmc);
        fprintf(f, "written=%s\n", when);
        ok = fclose(f) == 0;
    }

    printf("\n  Patchbay system info\n\n");
    printf("  Firmware:    %s\n", fw);
    printf("  Atmosphere:  %s\n", ams[0] ? ams : "(not found)");
    printf("  Running on:  %s\n\n", mmc);
    printf(ok ? "  Saved for Patchbay. You can go back to the Mac now.\n" : "  Couldn't save the file to the SD card.\n");
    printf("\n  Press + to close (closes by itself in 10 seconds).\n");

    u64 start = armGetSystemTick();
    while (appletMainLoop()) {
        padUpdate(&pad);
        if (padGetButtonsDown(&pad) & HidNpadButton_Plus) break;
        if (armTicksToNs(armGetSystemTick() - start) > 10000000000ULL) break;
        consoleUpdate(NULL);
    }
    consoleExit(NULL);
    return 0;
}
