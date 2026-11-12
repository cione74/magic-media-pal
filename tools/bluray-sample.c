#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libbluray/bluray.h>

#define SAMPLE_BYTES (8 * 1024 * 1024)

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("Dexter Blu-ray sample reader");
        return 0;
    }
    if (argc != 4) {
        fputs("Expected drive, playlist, and sample path\n", stderr);
        return 1;
    }
    char *end;
    errno = 0;
    long playlist = strtol(argv[2], &end, 10);
    if (errno || end == argv[2] || *end || playlist < 0 || playlist > 99999)
        return 1;

    BLURAY *disc = bd_open(argv[1], NULL);
    if (!disc) {
        fputs("Could not open the Blu-ray disc\n", stderr);
        return 1;
    }
    const BLURAY_DISC_INFO *info = bd_get_disc_info(disc);
    if (info && info->aacs_detected && !info->aacs_handled) {
        fputs("Could not initialize Blu-ray decryption\n", stderr);
        bd_close(disc);
        return 1;
    }
    if (!bd_select_playlist(disc, (uint32_t)playlist)) {
        fputs("Could not open the selected playlist\n", stderr);
        bd_close(disc);
        return 1;
    }

    FILE *sample = fopen(argv[3], "wb");
    if (!sample) {
        bd_close(disc);
        return 1;
    }
    unsigned char bytes[6144];
    size_t written = 0;
    int result = 0;
    while (written < SAMPLE_BYTES) {
        int count = bd_read(disc, bytes, sizeof(bytes));
        if (count < 0) {
            fputs("Could not read a decrypted sample\n", stderr);
            result = 1;
            break;
        }
        if (count == 0)
            break;
        if (fwrite(bytes, 1, (size_t)count, sample) != (size_t)count) {
            result = 1;
            break;
        }
        written += (size_t)count;
    }
    if (fclose(sample) != 0)
        result = 1;
    bd_close(disc);
    return written ? result : 1;
}
