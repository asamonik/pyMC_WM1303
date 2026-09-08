/*
 * capture_thread_singleread.c — CAPTUREWRAP Single-Read Clean-Stream
 *
 * Strategy:
 *   - CAPTUREWRAP=1: SX1302 writes continuously to circular 4096-sample buffer
 *   - ONE SPI read per cycle → simple, maximum HAL time
 *   - Record wp_before + wp_after → exact torn zone known
 *   - Decoder extracts ONLY clean zone, stitches via overlap verification
 *   - Result: 100% phase-coherent, zero torn-zone data in output
 *
 * Timing (28 MHz SPI, period=255, FS=125kHz):
 *   SPI read:    5.7 ms
 *   HAL pause:   5.0 ms
 *   Cycle:      10.7 ms  (< 32.8ms buffer → 22.1ms margin)
 *   HAL duty:   47% per cycle
 *
 * Clean zone per read: 4096 - 712 = 3384 samples
 * WP advance per cycle: 1338 samples
 * Overlap: 3384 - 1338 = 2046 samples → verified identical
 * Coverage: 100% — torn zone of cycle N is clean in cycle N+1
 *
 * UDP format v5: single read per packet, wp_before + wp_after
 *
 * Copyright 2026, pyMC WM1303 Project
 * SPDX-License-Identifier: BSD-3-Clause
 */

/*
 * ----------------------------------------------------------------------
 * pyMC_WM1303 — WM1303 Repeater adaptations
 * ----------------------------------------------------------------------
 * Copyright (c) 2026 HansvanMeer  (GitHub: @HansvanMeer)
 *
 * Licensed under the PolyForm Noncommercial License 1.0.0.
 * See LICENSE and COMMERCIAL.md in the pyMC_WM1303 repository:
 *   https://github.com/HansvanMeer/pyMC_WM1303
 *
 * Any portions of this file derived from Semtech's sx1302_hal remain
 * under Semtech's Revised BSD License (where applicable, see header
 * above). Modifications and original additions in this file are
 * licensed under PolyForm Noncommercial 1.0.0.
 *
 * Commercial use is NOT permitted without a separate written agreement.
 * See COMMERCIAL.md for commercial licensing inquiries.
 * ----------------------------------------------------------------------
 */

#define _POSIX_C_SOURCE 199309L
#define _GNU_SOURCE
#define _DEFAULT_SOURCE

#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <pthread.h>
#include <errno.h>
#include <time.h>
#include <math.h>
#include <sched.h>
#include <fcntl.h>
#include <sys/ioctl.h>

/* Networking */
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* SPI */
#include <linux/spi/spidev.h>

/* HAL includes */
#include "loragw_reg.h"
#include "loragw_hal.h"
#include "loragw_aux.h"
#include "loragw_com.h"
#include "loragw_spi.h"
#include "parson.h"

#include "capture_thread.h"

/* ---- Constants ---- */
#define CAPTURE_RAM_SIZE            16384   /* 4096 samples × 4 bytes */
#define CAP_SAMPLES                 4096    /* number of IQ samples per capture */

/* Register addresses */
#define REG_CAPTURE_CFG_ENABLE          1030
#define REG_CAPTURE_CFG_CAPTUREWRAP     1031
#define REG_CAPTURE_CFG_FORCETRIGGER    1032
#define REG_CAPTURE_CFG_CAPTURESTART    1033
#define REG_CAPTURE_CFG_RAMCONFIG       1034
#define REG_CAPTURE_SOURCE_A_SOURCEMUX  1035
#define REG_CAPTURE_PERIOD_0            1037
#define REG_CAPTURE_PERIOD_1            1038
#define REG_LAST_RAM_ADDR_0             1040
#define REG_LAST_RAM_ADDR_1             1041

/* Tuning */
#define SPI_SPEED_CAPTURE   28000000   /* 28 MHz for capture reads */
#define HAL_PAUSE_MS        5          /* ms HAL gets SPI between reads */
#define BURST_CYCLES        15         /* capture cycles per burst */
#define BURST_PAUSE_MS      200        /* ms HAL pause between bursts */

/*
 * UDP v5 header (20 bytes):
 * Offset  Len  Field
 * 0       1    version (0x05)
 * 1       1    flags (bit0=wrap)
 * 2       2    frame_count (u16 LE)
 * 4       4    timestamp_ms (u32 LE)
 * 8       2    wp_before (u16 LE)
 * 10      2    wp_after (u16 LE)
 * 12      2    capture_period (u16 LE)
 * 14      1    source
 * 15      1    reserved
 * 16      2    buf_samples (u16 LE)
 * 18      2    spi_speed_mhz (u16 LE)
 * ----
 * 20      16384  IQ data (4096 × 4 bytes)
 * ----
 * Total: 16404 bytes
 */
#define UDP_HDR_V5_SIZE     20
#define UDP_PKT_SIZE        (UDP_HDR_V5_SIZE + CAPTURE_RAM_SIZE)  /* 16404 */

/* ---- Globals ---- */
extern pthread_mutex_t mx_concent;
static volatile bool capture_thread_running = false;
static int capture_spi_fd = -1;

capture_conf_t capture_conf = {
    .enable = false,
    .source = 11,
    .period = 255,
    .udp_port = 1731,
};

/* ---- Helpers ---- */
static uint32_t get_timestamp_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void sleep_us(uint32_t us) {
    struct timespec ts = { .tv_sec = us / 1000000, .tv_nsec = (us % 1000000) * 1000L };
    nanosleep(&ts, NULL);
}

static uint32_t get_timestamp_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000000 + ts.tv_nsec / 1000);
}

/* ---- Direct SPI for capture RAM ---- */
static int open_capture_spi(void) {
    int fd = open("/dev/spidev0.0", O_RDWR);
    if (fd < 0) {
        printf("ERROR: [capture] open SPI: %s\n", strerror(errno));
        return -1;
    }
    uint8_t mode = SPI_MODE_0;
    ioctl(fd, SPI_IOC_WR_MODE, &mode);
    uint8_t bits = 8;
    ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    uint32_t speed = SPI_SPEED_CAPTURE;
    ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);
    printf("INFO: [capture] SPI device opened at %u MHz\n", speed / 1000000);
    return fd;
}



static int bulk_spi_read_ram(int fd, uint8_t *buf, int len) {
    int total = 4 + len;
    uint8_t *tx = calloc(total, 1);
    uint8_t *rx = calloc(total, 1);
    if (!tx || !rx) { free(tx); free(rx); return -1; }

    /* SPI command header for burst read at address 0x0000 (page already selected) */
    tx[0] = 0x00;  /* mux target = SX1302 */
    tx[1] = 0x00;  /* READ_ACCESS | addr[15:8]=0x00 */
    tx[2] = 0x00;  /* addr[7:0]=0x00 */
    tx[3] = 0x00;  /* dummy */

    struct spi_ioc_transfer xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.tx_buf = (unsigned long)tx;
    xfer.rx_buf = (unsigned long)rx;
    xfer.len = total;
    xfer.speed_hz = SPI_SPEED_CAPTURE;
    xfer.bits_per_word = 8;
    xfer.cs_change = 0;

    int ret = ioctl(fd, SPI_IOC_MESSAGE(1), &xfer);
    if (ret != total) {
        if (ret < 0) {
            perror("ERROR: [capture] ioctl SPI bulk read");
        } else {
            fprintf(stderr, "ERROR: [capture] short SPI bulk read: %d/%d bytes\n", ret, total);
        }
        free(tx); free(rx);
        return -1;
    }

    memcpy(buf, rx + 4, len);
    free(tx);
    free(rx);
    return 0;
}

/*
 * Read capture RAM with page-verify-and-retry.
 * If the HAL changes the page register during our read, the data is corrupt.
 * We detect this by checking the page register after reading and retry if needed.
 */
static int read_capture_ram(uint8_t *buf) {
    int32_t page_val;
    int retries = 0;
    const int MAX_RETRIES = 3;

    do {
        /* Switch to page 1 where CAPTURE_RAM data lives */
        int read_status = lgw_reg_w(SX1302_REG_COMMON_PAGE_PAGE, 1);
        if (read_status == LGW_REG_SUCCESS) {
            /* Direct SPI bulk read at 28MHz */
            read_status = bulk_spi_read_ram(capture_spi_fd, buf, CAPTURE_RAM_SIZE);
        }
        if (read_status == LGW_REG_SUCCESS) {
            /* Verify page is still 1 (HAL didn't change it during our read) */
            read_status = lgw_reg_r(SX1302_REG_COMMON_PAGE_PAGE, &page_val);
        }
        /* Restore page 0 even when a read failed. Never publish failed reads. */
        int restore_status = lgw_reg_w(SX1302_REG_COMMON_PAGE_PAGE, 0);
        if (read_status != LGW_REG_SUCCESS || restore_status != LGW_REG_SUCCESS) {
            return -1;
        }

        if (page_val == 1) {
            return 0;  /* Page was stable — data is good */
        }
        retries++;
        if (retries <= MAX_RETRIES) {
            printf("WARN: [capture] page race detected (page=%d), retry %d/%d\n",
                   (int)page_val, retries, MAX_RETRIES);
        }
    } while (retries <= MAX_RETRIES);

    if (retries > MAX_RETRIES) {
        printf("WARN: [capture] page race persisted after %d retries\n", MAX_RETRIES);
    }
    return -1;
}







static int read_write_ptr(uint16_t *write_ptr) {
    int32_t lo = 0, hi = 0;
    if (lgw_reg_r(REG_LAST_RAM_ADDR_0, &lo) != LGW_REG_SUCCESS ||
        lgw_reg_r(REG_LAST_RAM_ADDR_1, &hi) != LGW_REG_SUCCESS) {
        return -1;
    }
    *write_ptr = (uint16_t)((hi << 8) | (lo & 0xFF));
    return 0;
}

/* ---- Send UDP v5 packet ---- */
static void send_udp_v5(int sock, struct sockaddr_in *dst,
                        uint8_t *ram, uint16_t wp_before, uint16_t wp_after,
                        uint32_t ts_ms, uint32_t seq) {
    static uint8_t pkt[UDP_PKT_SIZE];
    uint16_t seq16 = (uint16_t)(seq & 0xFFFF);
    uint16_t period = capture_conf.period;
    uint8_t source = capture_conf.source;
    uint8_t flags = 0x01;  /* bit0=wrap */
    uint16_t buf_samples = (uint16_t)CAP_SAMPLES;
    uint16_t spi_mhz = (uint16_t)(SPI_SPEED_CAPTURE / 1000000);

    /* Header (20 bytes) */
    pkt[0] = 0x05;                        /* version 5 = single-read */
    pkt[1] = flags;
    memcpy(&pkt[2],  &seq16, 2);          /* frame_count LE */
    memcpy(&pkt[4],  &ts_ms, 4);          /* timestamp LE */
    memcpy(&pkt[8],  &wp_before, 2);      /* wp_before LE */
    memcpy(&pkt[10], &wp_after, 2);       /* wp_after LE */
    memcpy(&pkt[12], &period, 2);         /* capture_period LE */
    pkt[14] = source;                     /* source */
    pkt[15] = 0;                          /* reserved */
    memcpy(&pkt[16], &buf_samples, 2);    /* buf_samples LE */
    memcpy(&pkt[18], &spi_mhz, 2);       /* spi_speed_mhz LE */

    /* IQ data */
    memcpy(&pkt[UDP_HDR_V5_SIZE], ram, CAPTURE_RAM_SIZE);

    sendto(sock, pkt, UDP_PKT_SIZE, 0, (struct sockaddr*)dst, sizeof(*dst));
}

/* ---- Config parser ---- */
static void parse_capture_fields(JSON_Object *obj) {
    JSON_Value *val;
    val = json_object_get_value(obj, "enable");
    if (val) capture_conf.enable = json_value_get_boolean(val);
    val = json_object_get_value(obj, "source");
    if (val) capture_conf.source = (uint8_t)json_value_get_number(val);
    val = json_object_get_value(obj, "period");
    if (val) capture_conf.period = (uint16_t)json_value_get_number(val);
    val = json_object_get_value(obj, "udp_port");
    if (val) capture_conf.udp_port = (int)json_value_get_number(val);
}

void capture_conf_parse(JSON_Object *conf_obj) {
    JSON_Object *cap_obj = NULL;
    JSON_Value *file_root = NULL;
    const char *fallback = "/home/pi/wm1303_pf/capture_conf.json";

    if (conf_obj != NULL) {
        cap_obj = json_object_get_object(conf_obj, "capture_conf");
    }

    if (cap_obj != NULL) {
        printf("INFO: CAPTURE_RAM config from bridge_conf.json\n");
    } else {
        file_root = json_parse_file_with_comments(fallback);
        if (file_root != NULL) {
            JSON_Object *root_obj = json_value_get_object(file_root);
            if (root_obj != NULL) {
                cap_obj = json_object_get_object(root_obj, "capture_conf");
            }
            if (cap_obj != NULL) {
                printf("INFO: CAPTURE_RAM config from %s\n", fallback);
            }
        }
    }

    if (cap_obj == NULL) {
        printf("INFO: No CAPTURE_RAM config found, disabled\n");
        capture_conf.enable = false;
        if (file_root) json_value_free(file_root);
        return;
    }

    parse_capture_fields(cap_obj);

    double fs = 32000000.0 / (capture_conf.period + 1);
    double spi_ms = (double)(CAPTURE_RAM_SIZE + 4) * 8.0 / SPI_SPEED_CAPTURE * 1000.0;
    double cycle_ms = spi_ms + HAL_PAUSE_MS;
    double buf_ms = 4096.0 / fs * 1000.0;
    int torn = (int)(spi_ms / 1000.0 * fs);
    int clean = CAP_SAMPLES - torn;
    int wp_advance = (int)(cycle_ms / 1000.0 * fs);
    int overlap = clean - wp_advance;

    if (capture_conf.enable) {
        printf("INFO: CAPTURE_RAM SINGLE-READ clean-stream mode enabled\n");
        printf("INFO:   source=%u period=%u port=%d\n",
               capture_conf.source, capture_conf.period, capture_conf.udp_port);
        printf("INFO:   FS=%.1f Hz, buffer=%.2f ms\n", fs, buf_ms);
        printf("INFO:   SPI=%u MHz, read=%.1f ms\n",
               SPI_SPEED_CAPTURE / 1000000, spi_ms);
        printf("INFO:   torn_zone=~%d samples, clean_zone=~%d samples\n", torn, clean);
        printf("INFO:   cycle=%.1f ms (1 read + %d ms HAL)\n", cycle_ms, HAL_PAUSE_MS);
        printf("INFO:   wp_advance=~%d samples/cycle, overlap=~%d samples\n",
               wp_advance, overlap);
        printf("INFO:   margin=%.1f ms (buffer %.1f - cycle %.1f)\n",
               buf_ms - cycle_ms, buf_ms, cycle_ms);
        printf("INFO:   HAL duty: %.0f%% (%d ms / %.1f ms per cycle)\n",
               (double)HAL_PAUSE_MS / cycle_ms * 100.0, HAL_PAUSE_MS, cycle_ms);
        printf("INFO:   Burst: %d cycles + %dms pause → ~%.0f%% total HAL\n",
               BURST_CYCLES, BURST_PAUSE_MS,
               ((double)HAL_PAUSE_MS * BURST_CYCLES + BURST_PAUSE_MS) /
               (cycle_ms * BURST_CYCLES + BURST_PAUSE_MS) * 100.0);
    } else {
        printf("INFO: CAPTURE_RAM disabled\n");
    }

    if (file_root) json_value_free(file_root);
}

bool capture_conf_enabled(void) {
    return capture_conf.enable;
}

/* ---- Main capture thread: CAPTUREWRAP single-read ---- */
void *thread_capture_ram(void *arg) {
    (void)arg;

    /* Set real-time SCHED_FIFO priority for minimal jitter */
    struct sched_param sp;
    sp.sched_priority = 80;
    if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) == 0) {
        printf("INFO: [capture] Set SCHED_FIFO priority %d\n", sp.sched_priority);
    } else {
        printf("WARNING: [capture] Failed to set SCHED_FIFO: %s\n", strerror(errno));
    }

    /* Pin to CPU core 3 for isolation */
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(3, &cpuset);
    if (pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset) == 0) {
        printf("INFO: [capture] Pinned to CPU core 3\n");
    } else {
        printf("WARNING: [capture] Failed to pin to core 3: %s\n", strerror(errno));
    }

    int sock;
    struct sockaddr_in dst;
    uint8_t ram[CAPTURE_RAM_SIZE];
    uint16_t wp_before, wp_after;
    uint32_t seq = 0;

    double fs_raw = 32000000.0 / (capture_conf.period + 1);
    double buf_ms = (4096.0 / fs_raw) * 1000.0;
    double spi_ms = (double)(CAPTURE_RAM_SIZE + 4) * 8.0 / SPI_SPEED_CAPTURE * 1000.0;
    int torn_samples = (int)(spi_ms / 1000.0 * fs_raw);
    int clean_samples = CAP_SAMPLES - torn_samples;
    double cycle_ms = spi_ms + HAL_PAUSE_MS;
    int wp_advance = (int)(cycle_ms / 1000.0 * fs_raw);
    int overlap = clean_samples - wp_advance;

    printf("INFO: [capture] SINGLE-READ clean-stream thread started\n");
    printf("INFO: [capture]   source=%u period=%u port=%d\n",
           capture_conf.source, capture_conf.period, capture_conf.udp_port);
    printf("INFO: [capture]   FS=%.1f Hz, buffer=%.2f ms\n", fs_raw, buf_ms);
    printf("INFO: [capture]   SPI=%u MHz, read=%.1f ms, torn=~%d, clean=~%d\n",
           SPI_SPEED_CAPTURE / 1000000, spi_ms, torn_samples, clean_samples);
    printf("INFO: [capture]   cycle=%.1f ms, wp_advance=~%d, overlap=~%d\n",
           cycle_ms, wp_advance, overlap);
    printf("INFO: [capture]   HAL_PAUSE=%d ms, HAL duty=%.0f%%\n",
           HAL_PAUSE_MS, (double)HAL_PAUSE_MS / cycle_ms * 100.0);
    printf("INFO: [capture]   Burst: %d cycles + %d ms pause\n",
           BURST_CYCLES, BURST_PAUSE_MS);

    /* Open direct SPI device */
    capture_spi_fd = open_capture_spi();
    if (capture_spi_fd < 0) {
        printf("ERROR: [capture] failed to open SPI device, aborting\n");
        return NULL;
    }

    /* Create UDP socket */
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        printf("ERROR: [capture] cannot create UDP socket: %s\n", strerror(errno));
        close(capture_spi_fd);
        return NULL;
    }

    int sndbuf = 131072;  /* 128 KB — smaller than triple-read */
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    memset(&dst, 0, sizeof(dst));
    dst.sin_family = AF_INET;
    dst.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    dst.sin_port = htons(capture_conf.udp_port);

    /* One-time capture configuration (under mutex) */
    pthread_mutex_lock(&mx_concent);

    /* Stop any running capture */
    lgw_reg_w(REG_CAPTURE_CFG_CAPTURESTART, 0);
    lgw_reg_w(REG_CAPTURE_CFG_ENABLE, 0);
    usleep(1000);

    /* Configure: WRAP mode, single source A */
    lgw_reg_w(REG_CAPTURE_CFG_RAMCONFIG, 0x00);
    lgw_reg_w(REG_CAPTURE_SOURCE_A_SOURCEMUX, capture_conf.source);
    lgw_reg_w(REG_CAPTURE_PERIOD_0, capture_conf.period & 0xFF);
    lgw_reg_w(REG_CAPTURE_PERIOD_1, (capture_conf.period >> 8) & 0x03);
    lgw_reg_w(REG_CAPTURE_CFG_CAPTUREWRAP, 1);    /* WRAP MODE */
    lgw_reg_w(REG_CAPTURE_CFG_ENABLE, 1);

    /* Start capture — runs continuously */
    lgw_reg_w(REG_CAPTURE_CFG_FORCETRIGGER, 1);
    lgw_reg_w(REG_CAPTURE_CFG_CAPTURESTART, 1);

    pthread_mutex_unlock(&mx_concent);

    printf("INFO: [capture] CAPTUREWRAP started — single-read clean-stream\n");

    capture_thread_running = true;

    uint32_t cycle_count = 0;
    uint32_t burst_count = 0;
    uint32_t total_read_us = 0;
    uint32_t total_cycle_us = 0;

    while (capture_thread_running) {
        uint32_t t_cycle_start = get_timestamp_us();

        /* ===== SINGLE READ ===== */
        pthread_mutex_lock(&mx_concent);
        int read_status = read_write_ptr(&wp_before);
        if (read_status == 0) {
            read_status = read_capture_ram(ram);
        }
        if (read_status == 0) {
            read_status = read_write_ptr(&wp_after);
        }
        pthread_mutex_unlock(&mx_concent);
        if (read_status != 0) {
            sleep_us(HAL_PAUSE_MS * 1000);
            continue;
        }

        uint32_t ts_ms = get_timestamp_ms();
        uint32_t t_after_read = get_timestamp_us();
        uint32_t read_us = t_after_read - t_cycle_start;
        total_read_us += read_us;

        /* ===== Send UDP v5 ===== */
        seq++;
        send_udp_v5(sock, &dst, ram, wp_before, wp_after, ts_ms, seq);

        /* ===== HAL PAUSE ===== */
        sleep_us(HAL_PAUSE_MS * 1000);

        uint32_t t_cycle_end = get_timestamp_us();
        uint32_t cycle_us_val = t_cycle_end - t_cycle_start;
        total_cycle_us += cycle_us_val;
        cycle_count++;
        burst_count++;

        /* Burst mode: long pause every BURST_CYCLES for HAL main RX */
        if (burst_count >= BURST_CYCLES) {
            usleep(BURST_PAUSE_MS * 1000);
            burst_count = 0;
        }

        /* Log every 20 cycles */
        if (cycle_count % 20 == 0) {
            float avg_read_ms = (float)total_read_us / 20.0f / 1000.0f;
            float avg_cycle_ms = (float)total_cycle_us / 20.0f / 1000.0f;
            float spi_pct = avg_read_ms / avg_cycle_ms * 100.0f;

            printf("INFO: [capture] cyc=%u read=%.1fms cycle=%.1fms spi=%.0f%% wp=[%u→%u] torn=%u\n",
                   cycle_count, avg_read_ms, avg_cycle_ms, spi_pct,
                   wp_before, wp_after,
                   (wp_after - wp_before) & 0x0FFF);

            total_read_us = 0;
            total_cycle_us = 0;
        }
    }

    /* Cleanup */
    printf("INFO: [capture] thread stopping (seq=%u)\n", seq);

    pthread_mutex_lock(&mx_concent);
    lgw_reg_w(REG_CAPTURE_CFG_CAPTURESTART, 0);
    lgw_reg_w(REG_CAPTURE_CFG_ENABLE, 0);
    pthread_mutex_unlock(&mx_concent);

    close(sock);
    if (capture_spi_fd >= 0) {
        close(capture_spi_fd);
        capture_spi_fd = -1;
    }

    printf("INFO: [capture] thread stopped\n");
    return NULL;
}

void capture_thread_stop(void) {
    capture_thread_running = false;
}
