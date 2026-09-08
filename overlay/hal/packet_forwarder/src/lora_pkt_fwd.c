/*
 / _____)             _              | |
( (____  _____ ____ _| |_ _____  ____| |__
 \____ \| ___ |    (_   _) ___ |/ ___)  _ \
 _____) ) ____| | | || |_| ____( (___| | | |
(______/|_____)_|_|_| \__)_____)\____)_| |_|
  (C)2019 Semtech

Description:
    Configure Lora concentrator and forward packets to a server
    Use GPS for packet timestamping.
    Send a becon at a regular interval without server intervention

License: Revised BSD License, see LICENSE.TXT file include in the project
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

/* -------------------------------------------------------------------------- */
/* --- DEPENDANCIES --------------------------------------------------------- */

/* fix an issue between POSIX and C99 */
#if __STDC_VERSION__ >= 199901L
    #define _XOPEN_SOURCE 600
#else
    #define _XOPEN_SOURCE 500
#endif

#include <stdint.h>         /* C99 types */
#include <stdbool.h>        /* bool type */
#include <stdio.h>          /* printf, fprintf, snprintf, fopen, fputs */
#include <inttypes.h>       /* PRIx64, PRIu64... */

#include <string.h>         /* memset */
#include <signal.h>         /* sigaction */
#include <time.h>           /* time, clock_gettime, strftime, gmtime */
#include <sys/time.h>       /* timeval */
#include <unistd.h>         /* getopt, access */
#include <stdlib.h>         /* atoi, exit */
#include <errno.h>          /* error messages */
#include <math.h>           /* modf */

#include <sys/socket.h>     /* socket specific definitions */
#include <netinet/in.h>     /* INET constants and stuff */
#include <arpa/inet.h>      /* IP address conversion stuff */
#include <netdb.h>          /* gai_strerror */

#include <pthread.h>

#include "trace.h"
#include "jitqueue.h"
#include "parson.h"
#include "base64.h"
#include "loragw_hal.h"
#include "loragw_aux.h"
#include "loragw_sx1302.h"  /* WM1303: for SX1302 register monitoring */
#include "loragw_sx1250.h"  /* WM1303: for SX1250 status monitoring */
#include "sx1250_defs.h"    /* WM1303: SX1250 opcode definitions */
#include "loragw_reg.h"
#include "loragw_gps.h"
#include "loragw_sx1261.h"  /* for sx1261_cad_scan() */
#include "loragw_lbt.h"     /* for lgw_lbt_start/lgw_lbt_tx_status (HAL LBT) */
#include "capture_thread.h" /* for CAPTURE_RAM streaming */
#include "loragw_sx1302.h"  /* WM1303: for SX1302 register monitoring */

/* -------------------------------------------------------------------------- */
/* --- PRIVATE MACROS ------------------------------------------------------- */

#define ARRAY_SIZE(a)   (sizeof(a) / sizeof((a)[0]))
#define STRINGIFY(x)    #x
#define STR(x)          STRINGIFY(x)

#define RAND_RANGE(min, max) (rand() % (max + 1 - min) + min)

/* -------------------------------------------------------------------------- */
/* --- PRIVATE CONSTANTS ---------------------------------------------------- */

#ifndef VERSION_STRING
    #define VERSION_STRING "undefined"
#endif

#define JSON_CONF_DEFAULT   "global_conf.json"

#define DEFAULT_SERVER      127.0.0.1   /* hostname also supported */
#define DEFAULT_PORT_UP     1780
#define DEFAULT_PORT_DW     1782
#define DEFAULT_KEEPALIVE   5           /* default time interval for downstream keep-alive packet */
#define DEFAULT_STAT        30          /* default time interval for statistics */
#define PUSH_TIMEOUT_MS     100
#define PULL_TIMEOUT_MS     200
#define GPS_REF_MAX_AGE     30          /* maximum admitted delay in seconds of GPS loss before considering latest GPS sync unusable */
#define FETCH_SLEEP_MS      1           /* WM1303: reduced from 10ms to 1ms for faster TX pickup */
#define BEACON_POLL_MS      50          /* time in ms between polling of beacon TX status */

#define PROTOCOL_VERSION    2           /* v1.6 */
#define PROTOCOL_JSON_RXPK_FRAME_FORMAT 1

#define XERR_INIT_AVG       16          /* nb of measurements the XTAL correction is averaged on as initial value */
#define XERR_FILT_COEF      256         /* coefficient for low-pass XTAL error tracking */

#define PKT_PUSH_DATA   0
#define PKT_PUSH_ACK    1
#define PKT_PULL_DATA   2
#define PKT_PULL_RESP   3
#define PKT_PULL_ACK    4
#define PKT_TX_ACK      5

#define NB_PKT_MAX      255 /* max number of packets per fetch/send cycle */

#define MIN_LORA_PREAMB 6 /* minimum Lora preamble length for this application */
#define STD_LORA_PREAMB 8
#define MIN_FSK_PREAMB  3 /* minimum FSK preamble length for this application */
#define STD_FSK_PREAMB  5

#define STATUS_SIZE     200
#define TX_BUFF_SIZE    ((540 * NB_PKT_MAX) + 30 + STATUS_SIZE)
#define ACK_BUFF_SIZE   512  /* WM1303: increased from 64 to fit post-TX CAD/LBT JSON */

#define UNIX_GPS_EPOCH_OFFSET 315964800 /* Number of seconds ellapsed between 01.Jan.1970 00:00:00
                                                                          and 06.Jan.1980 00:00:00 */

#define DEFAULT_BEACON_FREQ_HZ      869525000
#define DEFAULT_BEACON_FREQ_NB      1
#define DEFAULT_BEACON_FREQ_STEP    0
#define DEFAULT_BEACON_DATARATE     9
#define DEFAULT_BEACON_BW_HZ        125000
#define DEFAULT_BEACON_POWER        14
#define DEFAULT_BEACON_INFODESC     0

/* -------------------------------------------------------------------------- */
/* --- PRIVATE TYPES -------------------------------------------------------- */

/* spectral scan */
typedef struct spectral_scan_s {
    bool enable;            /* enable spectral scan thread */
    uint32_t freq_hz_start; /* first channel frequency, in Hz */
    uint8_t nb_chan;        /* number of channels to scan (200kHz between each channel) */
    uint16_t nb_scan;       /* number of scan points for each frequency scan */
    uint32_t pace_s;        /* number of seconds between 2 scans in the thread */
} spectral_scan_t;

/* CAD (Channel Activity Detection) configuration for hardware CAD via SX1261 */
#define CAD_MAX_CHANNELS 4
typedef struct cad_channel_s {
    uint32_t freq_hz;       /* channel center frequency in Hz */
    uint8_t  sf;            /* spreading factor (7-12) */
    uint8_t  bw;            /* bandwidth: 0=125kHz, 1=250kHz, 2=500kHz */
    char     id[32];        /* channel identifier string */
} cad_channel_t;

typedef struct cad_config_s {
    bool            enable;
    int             nb_channels;
    cad_channel_t   channels[CAD_MAX_CHANNELS];
} cad_config_t;

static cad_config_t cad_config = { .enable = false, .nb_channels = 0 };

/* CAD results storage (written to JSON after each sweep) */
static sx1261_cad_result_t cad_results[CAD_MAX_CHANNELS];

/**
 * @brief Read CAD channel configuration from /tmp/pymc_cad_config.json
 * @return number of channels loaded, or 0 if no config
 */
static int cad_read_config(void) {
    JSON_Value *root_val = NULL;
    JSON_Object *root_obj = NULL;
    JSON_Array *ch_array = NULL;
    int nb_ch, i;

    root_val = json_parse_file("/tmp/pymc_cad_config.json");
    if (root_val == NULL) {
        cad_config.enable = false;
        cad_config.nb_channels = 0;
        return 0;
    }

    root_obj = json_value_get_object(root_val);
    if (root_obj == NULL) {
        json_value_free(root_val);
        cad_config.enable = false;
        cad_config.nb_channels = 0;
        return 0;
    }

    ch_array = json_object_get_array(root_obj, "channels");
    if (ch_array == NULL) {
        json_value_free(root_val);
        cad_config.enable = false;
        cad_config.nb_channels = 0;
        return 0;
    }

    nb_ch = (int)json_array_get_count(ch_array);
    if (nb_ch > CAD_MAX_CHANNELS) nb_ch = CAD_MAX_CHANNELS;

    cad_config.nb_channels = 0;
    for (i = 0; i < nb_ch; i++) {
        JSON_Object *ch_obj = json_array_get_object(ch_array, i);
        if (ch_obj == NULL) continue;

        const char *id = json_object_get_string(ch_obj, "id");
        double freq = json_object_get_number(ch_obj, "freq_hz");
        double sf_val = json_object_get_number(ch_obj, "sf");
        double bw_val = json_object_get_number(ch_obj, "bw");

        if (freq < 800e6 || freq > 930e6 || sf_val < 7 || sf_val > 12) {
            printf("WARNING: CAD config: skipping invalid channel %s (freq=%.0f, sf=%.0f)\n",
                   id ? id : "?", freq, sf_val);
            continue;
        }

        cad_config.channels[cad_config.nb_channels].freq_hz = (uint32_t)freq;
        cad_config.channels[cad_config.nb_channels].sf = (uint8_t)sf_val;
        cad_config.channels[cad_config.nb_channels].bw = (uint8_t)bw_val;
        if (id != NULL) {
            strncpy(cad_config.channels[cad_config.nb_channels].id, id, 31);
            cad_config.channels[cad_config.nb_channels].id[31] = '\0';
        } else {
            snprintf(cad_config.channels[cad_config.nb_channels].id, 32, "ch_%d", i);
        }
        cad_config.nb_channels++;
    }

    cad_config.enable = (cad_config.nb_channels > 0);
    json_value_free(root_val);

    printf("INFO: CAD config loaded: %d channels\n", cad_config.nb_channels);
    for (i = 0; i < cad_config.nb_channels; i++) {
        printf("INFO:   CAD channel %s: freq=%u, SF%u, BW%u\n",
               cad_config.channels[i].id,
               cad_config.channels[i].freq_hz,
               cad_config.channels[i].sf,
               cad_config.channels[i].bw);
    }

    return cad_config.nb_channels;
}


/* -------------------------------------------------------------------------- */
/* --- PRIVATE VARIABLES (GLOBAL) ------------------------------------------- */

/* signal handling variables */
volatile bool exit_sig = false; /* 1 -> application terminates cleanly (shut down hardware, close open files, etc) */
volatile bool quit_sig = false; /* 1 -> application terminates without shutting down the hardware */

/* packets filtering configuration variables */
static bool fwd_valid_pkt = true; /* packets with PAYLOAD CRC OK are forwarded */
static bool fwd_error_pkt = false; /* packets with PAYLOAD CRC ERROR are NOT forwarded */
static bool fwd_nocrc_pkt = false; /* packets with NO PAYLOAD CRC are NOT forwarded */

/* network configuration variables */
static uint64_t lgwm = 0; /* Lora gateway MAC address */
static char serv_addr[64] = STR(DEFAULT_SERVER); /* address of the server (host name or IPv4/IPv6) */
static char serv_port_up[8] = STR(DEFAULT_PORT_UP); /* server port for upstream traffic */
static char serv_port_down[8] = STR(DEFAULT_PORT_DW); /* server port for downstream traffic */
static int keepalive_time = DEFAULT_KEEPALIVE; /* send a PULL_DATA request every X seconds, negative = disabled */

/* statistics collection configuration variables */
static unsigned stat_interval = DEFAULT_STAT; /* time interval (in sec) at which statistics are collected and displayed */

/* gateway <-> MAC protocol variables */
static uint32_t net_mac_h; /* Most Significant Nibble, network order */
static uint32_t net_mac_l; /* Least Significant Nibble, network order */

/* network sockets */
static int sock_up; /* socket for upstream traffic */
static int sock_down; /* socket for downstream traffic */

/* network protocol variables */
static struct timeval push_timeout_half = {0, (PUSH_TIMEOUT_MS * 500)}; /* cut in half, critical for throughput */
static struct timeval pull_timeout = {0, (PULL_TIMEOUT_MS * 1000)}; /* non critical for throughput */

/* hardware access control and correction */
pthread_mutex_t mx_concent = PTHREAD_MUTEX_INITIALIZER; /* control access to the concentrator */
static pthread_mutex_t mx_xcorr = PTHREAD_MUTEX_INITIALIZER; /* control access to the XTAL correction */
static bool xtal_correct_ok = false; /* set true when XTAL correction is stable enough */
static double xtal_correct = 1.0;

/* GPS configuration and synchronization */
static char gps_tty_path[64] = "\0"; /* path of the TTY port GPS is connected on */
static int gps_tty_fd = -1; /* file descriptor of the GPS TTY port */
static bool gps_enabled = false; /* is GPS enabled on that gateway ? */

/* GPS time reference */
static pthread_mutex_t mx_timeref = PTHREAD_MUTEX_INITIALIZER; /* control access to GPS time reference */
static bool gps_ref_valid; /* is GPS reference acceptable (ie. not too old) */
static struct tref time_reference_gps; /* time reference used for GPS <-> timestamp conversion */

/* Reference coordinates, for broadcasting (beacon) */
static struct coord_s reference_coord;

/* Enable faking the GPS coordinates of the gateway */
static bool gps_fake_enable; /* enable the feature */

/* measurements to establish statistics */
static pthread_mutex_t mx_meas_up = PTHREAD_MUTEX_INITIALIZER; /* control access to the upstream measurements */
static uint32_t meas_nb_rx_rcv = 0; /* count packets received */
static uint32_t meas_nb_rx_ok = 0; /* count packets received with PAYLOAD CRC OK */
static uint32_t meas_nb_rx_bad = 0; /* count packets received with PAYLOAD CRC ERROR */
static uint32_t meas_nb_rx_nocrc = 0; /* count packets received with NO PAYLOAD CRC */
static uint32_t meas_up_pkt_fwd = 0; /* number of radio packet forwarded to the server */
static uint32_t meas_up_network_byte = 0; /* sum of UDP bytes sent for upstream traffic */
static uint32_t meas_up_payload_byte = 0; /* sum of radio payload bytes sent for upstream traffic */
static uint32_t meas_up_dgram_sent = 0; /* number of datagrams sent for upstream traffic */
static uint32_t meas_up_ack_rcv = 0; /* number of datagrams acknowledged for upstream traffic */

static pthread_mutex_t mx_meas_dw = PTHREAD_MUTEX_INITIALIZER; /* control access to the downstream measurements */
static uint32_t meas_dw_pull_sent = 0; /* number of PULL requests sent for downstream traffic */
static uint32_t meas_dw_ack_rcv = 0; /* number of PULL requests acknowledged for downstream traffic */
static uint32_t meas_dw_dgram_rcv = 0; /* count PULL response packets received for downstream traffic */
static uint32_t meas_dw_network_byte = 0; /* sum of UDP bytes sent for upstream traffic */
static uint32_t meas_dw_payload_byte = 0; /* sum of radio payload bytes sent for upstream traffic */
static uint32_t meas_nb_tx_ok = 0; /* count packets emitted successfully */
static uint32_t meas_nb_tx_fail = 0; /* count packets were TX failed for other reasons */
static uint32_t meas_nb_tx_requested = 0; /* count TX request from server (downlinks) */
static uint32_t meas_nb_tx_rejected_collision_packet = 0; /* count packets were TX request were rejected due to collision with another packet already programmed */
static uint32_t meas_nb_tx_rejected_collision_beacon = 0; /* count packets were TX request were rejected due to collision with a beacon already programmed */
static uint32_t meas_nb_tx_rejected_too_late = 0; /* count packets were TX request were rejected because it is too late to program it */
static uint32_t meas_nb_tx_rejected_too_early = 0; /* count packets were TX request were rejected because timestamp is too much in advance */
static uint32_t meas_nb_beacon_queued = 0; /* count beacon inserted in jit queue */
static uint32_t meas_nb_beacon_sent = 0; /* count beacon actually sent to concentrator */
static uint32_t meas_nb_beacon_rejected = 0; /* count beacon rejected for queuing */

static pthread_mutex_t mx_meas_gps = PTHREAD_MUTEX_INITIALIZER; /* control access to the GPS statistics */
static bool gps_coord_valid; /* could we get valid GPS coordinates ? */
static struct coord_s meas_gps_coord; /* GPS position of the gateway */
static struct coord_s meas_gps_err; /* GPS position of the gateway */

static pthread_mutex_t mx_stat_rep = PTHREAD_MUTEX_INITIALIZER; /* control access to the status report */
static bool report_ready = false; /* true when there is a new report to send to the server */
static char status_report[STATUS_SIZE]; /* status report as a JSON object */

/* beacon parameters */
static uint32_t beacon_period = 0; /* set beaconing period, must be a sub-multiple of 86400, the nb of sec in a day */
static uint32_t beacon_freq_hz = DEFAULT_BEACON_FREQ_HZ; /* set beacon TX frequency, in Hz */
static uint8_t beacon_freq_nb = DEFAULT_BEACON_FREQ_NB; /* set number of beaconing channels beacon */
static uint32_t beacon_freq_step = DEFAULT_BEACON_FREQ_STEP; /* set frequency step between beacon channels, in Hz */
static uint8_t beacon_datarate = DEFAULT_BEACON_DATARATE; /* set beacon datarate (SF) */
static uint32_t beacon_bw_hz = DEFAULT_BEACON_BW_HZ; /* set beacon bandwidth, in Hz */
static int8_t beacon_power = DEFAULT_BEACON_POWER; /* set beacon TX power, in dBm */
static uint8_t beacon_infodesc = DEFAULT_BEACON_INFODESC; /* set beacon information descriptor */

/* auto-quit function */
static uint32_t autoquit_threshold = 0; /* enable auto-quit after a number of non-acknowledged PULL_DATA (0 = disabled)*/

/* Just In Time TX scheduling */
static struct jit_queue_s jit_queue[LGW_RF_CHAIN_NB];

/* Gateway specificities */
static int8_t antenna_gain = 0;

/* TX capabilities */
static struct lgw_tx_gain_lut_s txlut[LGW_RF_CHAIN_NB]; /* TX gain table */
static uint32_t tx_freq_min[LGW_RF_CHAIN_NB]; /* lowest frequency supported by TX chain */
static uint32_t tx_freq_max[LGW_RF_CHAIN_NB]; /* highest frequency supported by TX chain */
static bool tx_enable[LGW_RF_CHAIN_NB] = {false}; /* Is TX enabled for a given RF chain ? */

static uint32_t nb_pkt_log[LGW_IF_CHAIN_NB][8]; /* [CH][SF] */
static uint32_t nb_pkt_received_lora = 0;
static uint32_t nb_pkt_received_fsk = 0;

static struct lgw_conf_debug_s debugconf;
static uint32_t nb_pkt_received_ref[16];
static volatile uint64_t spectral_last_rx_ms = 0;
static volatile uint32_t spectral_last_rx_toa_ms = 0;

static uint64_t spectral_now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000ULL) + ((uint64_t)ts.tv_nsec / 1000000ULL);
}

static uint32_t spectral_compute_rx_toa_ms(const struct lgw_pkt_rx_s *p) {
    uint32_t toa_us = 0;
    if (p == NULL) return 0;
    if (p->modulation == MOD_LORA) {
        toa_us = lora_packet_time_on_air(p->bandwidth, p->datarate, p->coderate, 8, false, false, p->size, NULL, NULL, NULL);
        return (uint32_t)((toa_us + 999U) / 1000U);
    }
    return 0;
}


/* Interface type */
static lgw_com_type_t com_type = LGW_COM_SPI;

/* Spectral Scan */
static spectral_scan_t spectral_scan_params = {
    .enable = false,
    .freq_hz_start = 0,
    .nb_chan = 0,
    .nb_scan = 0,
    .pace_s = 10
};

/* Custom pre-TX checks: CAD only.
   Custom LBT was removed in favor of HAL LBT (per-channel rssi_target_dbm,
   handled inside lgw_send() via the SX1261). The remaining `custom_lbt_*`
   identifiers are kept to avoid a large rename; they now serve CAD and the
   generic post-TX RX-restart logic. */
static bool custom_cad_enable = false;  /* LoRa Channel Activity Detection */


/* Non-blocking LoRa RX restart after TX: stores the time at which
   the SX1261 LoRa RX should be restarted (after TX airtime completes).
   Checked at the top of each JIT loop iteration.
   NOTE: name retained from legacy custom-LBT era; now serves any post-TX RX restart. */
static struct timespec custom_lbt_rx_restart_after = {0, 0};
static bool custom_lbt_rx_restart_pending = false;

/* Per-channel CAD configuration (shared struct — LBT fields removed).
   Only freq_hz and cad_enabled remain; the array is still consumed
   elsewhere in the file to gate CAD on/off per channel. */
#define CUSTOM_LBT_MAX_CHANNELS 8
static struct {
    uint32_t freq_hz;
    bool cad_enabled;
} custom_lbt_channels[CUSTOM_LBT_MAX_CHANNELS];
static int custom_lbt_nb_channels = 0;

/* HAL LBT per-channel config snapshot (populated at startup from
   sx1261conf.lbt_conf). Used at TX time to look up the effective
   rssi_target_dbm and enabled state for post-TX TX_ACK reporting. */
#define HAL_LBT_MAX_CHANNELS 8
static struct {
    uint32_t freq_hz;
    uint8_t  bandwidth;
    int8_t   rssi_target_dbm;
    bool     enable;           /* per-channel LBT enable */
} hal_lbt_channels[HAL_LBT_MAX_CHANNELS];
static int  hal_lbt_nb_channels = 0;
static bool hal_lbt_enabled_global = false;
static int8_t hal_lbt_global_target_dbm = -80;

/* --- WM1303: Post-TX CAD/LBT result plumbing ---
   Extended TX_ACK carrying CAD/LBT outcomes from the JIT thread back to Python.
   The existing enqueue-time TX_ACK is kept unchanged for backwards compatibility;
   an additional ACK with "phase":"post_tx" is emitted after lgw_send() completes
   (or after a clbt_blocked skip). Token correlation uses a small table populated
   at enqueue time and drained at TX time. */
typedef struct {
    bool         cad_enabled;
    bool         cad_detected;      /* CAD detected activity in final attempt */
    uint8_t      cad_retries;       /* retries performed (0 = clear on first try) */
    int16_t      tx_noisefloor;     /* TX noise floor from pre-CAD FSK-RX (dBm) */
    const char * cad_reason;        /* "clear", "cleared_after_retries", "forced_after_retries",
                                       "scan_error", "unsupported_bw" */
    bool         lbt_enabled;
    int16_t      lbt_rssi_dbm;      /* last measured RSSI (dBm) */
    int8_t       lbt_threshold_dbm;
    bool         lbt_checked;       /* HAL returned a confirmed LBT decision */
    bool         lbt_pass;          /* valid only when lbt_checked is true */
    uint8_t      lbt_retries;
    uint32_t     cad_duration_ms;    /* total CAD scan time including retries (ms) */
    const char * tx_result;         /* "sent", "blocked", "send_failed" */
} tx_ack_extra_t;

#define TX_ACK_TOKEN_SLOTS 32
typedef struct {
    bool     used;
    uint32_t count_us;     /* txpkt.count_us (0 for IMMEDIATE) */
    uint32_t freq_hz;
    uint16_t size;
    uint8_t  datarate;
    uint8_t  bandwidth;
    uint8_t  token_h;
    uint8_t  token_l;
    struct timespec enq_ts;
} pending_tx_ack_t;

static pending_tx_ack_t tx_ack_tokens[LGW_RF_CHAIN_NB][TX_ACK_TOKEN_SLOTS];
static pthread_mutex_t mx_tx_ack_tokens = PTHREAD_MUTEX_INITIALIZER;


/* -------------------------------------------------------------------------- */
/* --- PRIVATE FUNCTIONS DECLARATION ---------------------------------------- */

static void usage(void);

static void sig_handler(int sigio);

static int parse_SX130x_configuration(const char * conf_file);

static int parse_gateway_configuration(const char * conf_file);

static int parse_debug_configuration(const char * conf_file);

static uint16_t crc16(const uint8_t * data, unsigned size);

static double difftimespec(struct timespec end, struct timespec beginning);

static void gps_process_sync(void);

static void gps_process_coords(void);

static int get_tx_gain_lut_index(uint8_t rf_chain, int8_t rf_power, uint8_t * lut_index);

/* threads */
void thread_up(void);
void thread_down(void);
void thread_jit(void);
void thread_gps(void);
void thread_valid(void);
void thread_spectral_scan(void);

/* -------------------------------------------------------------------------- */
/* --- PRIVATE FUNCTIONS DEFINITION ----------------------------------------- */

static void usage( void )
{
    printf("~~~ Library version string~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n");
    printf(" %s\n", lgw_version_info());
    printf("~~~ Available options ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n");
    printf(" -h  print this help\n");
    printf(" -c <filename>  use config file other than 'global_conf.json'\n");
    printf("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n");
}

/* (Removed: custom_lbt_find_channel — dead after custom LBT cleanup.
   HAL LBT now uses hal_lbt_lookup() below; CAD no longer needs a
   per-channel lookup because HW-CAD is run unconditionally.) */

/* Convert HAL bandwidth constant to sx1261_cad_scan bw parameter.
   Returns: 0=125kHz, 1=250kHz, 2=500kHz, 3=62.5kHz, or -1 if unsupported. */
static int custom_lbt_hal_bw_to_cad(uint8_t hal_bw) {
    switch (hal_bw) {
        case BW_125KHZ: return 0;
        case BW_250KHZ: return 1;
        case BW_500KHZ: return 2;
        case BW_62K5HZ: return 3;
        default:        return -1;
    }
}

/* HAL LBT: look up per-channel rssi_target_dbm by frequency and bandwidth.
   Populates *enabled and *threshold_dbm based on the snapshot of the HAL
   LBT config taken at startup. If the global LBT is disabled, returns
   enabled=false. Mirror loragw_lbt.c's first compatible match: ±10 kHz,
   exact BW or its existing 62.5 kHz TX / 125 kHz scan fallback. */
static void hal_lbt_lookup(uint32_t freq_hz, uint8_t bandwidth,
                           bool *enabled, int8_t *threshold_dbm) {
    if (enabled == NULL || threshold_dbm == NULL) return;
    if (!hal_lbt_enabled_global) {
        *enabled = false;
        *threshold_dbm = hal_lbt_global_target_dbm;
        return;
    }
    int i;
    for (i = 0; i < hal_lbt_nb_channels; i++) {
        uint32_t diff = (freq_hz > hal_lbt_channels[i].freq_hz)
                      ? (freq_hz - hal_lbt_channels[i].freq_hz)
                      : (hal_lbt_channels[i].freq_hz - freq_hz);
        bool bw_matches = (bandwidth == hal_lbt_channels[i].bandwidth) ||
                          (bandwidth == BW_62K5HZ && hal_lbt_channels[i].bandwidth == BW_125KHZ);
        if (diff <= 10000 && bw_matches) {
            *enabled = hal_lbt_channels[i].enable;  /* Use per-channel setting */
            *threshold_dbm = hal_lbt_channels[i].rssi_target_dbm;
            return;
        }
    }
    /* No per-channel entry — channel not configured for HAL LBT */
    *enabled = false;
    *threshold_dbm = hal_lbt_global_target_dbm;
}

/* Capture only confirmed outcomes while the caller still owns mx_concent.
   An ordinary HAL error does not establish either a pass or a busy channel. */
static void hal_lbt_capture_result(tx_ack_extra_t *extra, int send_result) {
    extra->lbt_checked = hal_lbt_enabled_global &&
                        (send_result == LGW_HAL_SUCCESS || send_result == LGW_LBT_NOT_ALLOWED);
    extra->lbt_pass = (send_result == LGW_HAL_SUCCESS);
    extra->lbt_rssi_dbm = -128;
    if (extra->lbt_checked) {
        int16_t rssi = -128;
        if (lgw_lbt_get_last_rssi(&rssi) == 0 && rssi > -128) {
            extra->lbt_rssi_dbm = rssi;
        }
    }
}

/* --- WM1303: Pending TX-ack token table helpers ---
   Called from the downlink thread at enqueue time, and from the JIT thread at
   TX time. Lookup matches by (count_us, freq_hz, size) which uniquely
   identifies a packet within the short window between enqueue and TX. For
   IMMEDIATE packets (count_us=0) we additionally require freq+size+datarate
   to match, and fall back to oldest-first if multiple hits. */
static void tx_ack_token_store(uint8_t rf_chain, uint8_t token_h, uint8_t token_l,
                               const struct lgw_pkt_tx_s *pkt) {
    if (rf_chain >= LGW_RF_CHAIN_NB || pkt == NULL) return;
    pthread_mutex_lock(&mx_tx_ack_tokens);
    /* Garbage-collect stale slots (>10s old) before inserting */
    struct timespec now_ts;
    clock_gettime(CLOCK_MONOTONIC, &now_ts);
    int free_slot = -1;
    for (int s = 0; s < TX_ACK_TOKEN_SLOTS; s++) {
        if (tx_ack_tokens[rf_chain][s].used) {
            double age = difftimespec(now_ts, tx_ack_tokens[rf_chain][s].enq_ts);
            if (age > 10.0) {
                tx_ack_tokens[rf_chain][s].used = false;
            }
        }
        if (!tx_ack_tokens[rf_chain][s].used && free_slot < 0) {
            free_slot = s;
        }
    }
    if (free_slot < 0) {
        /* No free slot — overwrite oldest to avoid permanent leak */
        double oldest = -1.0;
        int oldest_idx = 0;
        for (int s = 0; s < TX_ACK_TOKEN_SLOTS; s++) {
            double age = difftimespec(now_ts, tx_ack_tokens[rf_chain][s].enq_ts);
            if (age > oldest) { oldest = age; oldest_idx = s; }
        }
        free_slot = oldest_idx;
    }
    tx_ack_tokens[rf_chain][free_slot].used = true;
    tx_ack_tokens[rf_chain][free_slot].count_us = pkt->count_us;
    tx_ack_tokens[rf_chain][free_slot].freq_hz = pkt->freq_hz;
    tx_ack_tokens[rf_chain][free_slot].size = pkt->size;
    tx_ack_tokens[rf_chain][free_slot].datarate = pkt->datarate;
    tx_ack_tokens[rf_chain][free_slot].bandwidth = pkt->bandwidth;
    tx_ack_tokens[rf_chain][free_slot].token_h = token_h;
    tx_ack_tokens[rf_chain][free_slot].token_l = token_l;
    tx_ack_tokens[rf_chain][free_slot].enq_ts = now_ts;
    pthread_mutex_unlock(&mx_tx_ack_tokens);
}

/* Claim (remove) the token matching the given packet. Returns true if found. */
static bool tx_ack_token_claim(uint8_t rf_chain, const struct lgw_pkt_tx_s *pkt,
                               uint8_t *out_token_h, uint8_t *out_token_l) {
    if (rf_chain >= LGW_RF_CHAIN_NB || pkt == NULL ||
        out_token_h == NULL || out_token_l == NULL) return false;
    bool found = false;
    pthread_mutex_lock(&mx_tx_ack_tokens);
    int best = -1;
    struct timespec best_ts = {0, 0};
    for (int s = 0; s < TX_ACK_TOKEN_SLOTS; s++) {
        if (!tx_ack_tokens[rf_chain][s].used) continue;
        if (tx_ack_tokens[rf_chain][s].freq_hz != pkt->freq_hz) continue;
        if (tx_ack_tokens[rf_chain][s].size != pkt->size) continue;
        if (tx_ack_tokens[rf_chain][s].datarate != pkt->datarate) continue;
        if (tx_ack_tokens[rf_chain][s].bandwidth != pkt->bandwidth) continue;
        if (pkt->count_us != 0 &&
            tx_ack_tokens[rf_chain][s].count_us != pkt->count_us &&
            tx_ack_tokens[rf_chain][s].count_us != 0) continue;
        /* Pick the oldest matching entry (FIFO) */
        if (best < 0 || difftimespec(tx_ack_tokens[rf_chain][s].enq_ts, best_ts) < 0) {
            best = s;
            best_ts = tx_ack_tokens[rf_chain][s].enq_ts;
        }
    }
    if (best >= 0) {
        *out_token_h = tx_ack_tokens[rf_chain][best].token_h;
        *out_token_l = tx_ack_tokens[rf_chain][best].token_l;
        tx_ack_tokens[rf_chain][best].used = false;
        found = true;
    }
    pthread_mutex_unlock(&mx_tx_ack_tokens);
    return found;
}

/* Forward declaration (defined later in file) */
static int send_tx_ack(uint8_t token_h, uint8_t token_l, enum jit_error_e error, int32_t error_value, const tx_ack_extra_t *extra);

/* WM1303: Periodically sweep stale token slots and send TOO_LATE ACKs
   for packets that were enqueued but never transmitted (e.g. JIT drops).
   This prevents Python-side ACK timeouts (3+ seconds) by proactively
   notifying the caller within ~3 seconds of enqueue. */
static void tx_ack_token_sweep_stale(void) {
    struct timespec now_ts;
    clock_gettime(CLOCK_MONOTONIC, &now_ts);
    pthread_mutex_lock(&mx_tx_ack_tokens);
    for (int rf = 0; rf < LGW_RF_CHAIN_NB; rf++) {
        for (int s = 0; s < TX_ACK_TOKEN_SLOTS; s++) {
            if (!tx_ack_tokens[rf][s].used) continue;
            double age = difftimespec(now_ts, tx_ack_tokens[rf][s].enq_ts);
            if (age > 2.0) {
                uint8_t th = tx_ack_tokens[rf][s].token_h;
                uint8_t tl = tx_ack_tokens[rf][s].token_l;
                tx_ack_tokens[rf][s].used = false;
                /* Release mutex briefly to send ACK (uses UDP socket) */
                pthread_mutex_unlock(&mx_tx_ack_tokens);
                MSG("WARNING: [stale-ack] token 0x%02x%02x aged %.1fs — sending TOO_LATE ACK\n", th, tl, age);
                send_tx_ack(th, tl, JIT_ERROR_TOO_LATE, 0, NULL);
                /* Re-acquire and restart the inner loop for this rf chain */
                pthread_mutex_lock(&mx_tx_ack_tokens);
                s = -1; /* will be incremented to 0 by for loop */
            }
        }
    }
    pthread_mutex_unlock(&mx_tx_ack_tokens);
}



static void sig_handler(int sigio) {
    if (sigio == SIGQUIT) {
        quit_sig = true;
    } else if ((sigio == SIGINT) || (sigio == SIGTERM)) {
        exit_sig = true;
    }
    return;
}

static int parse_SX130x_configuration(const char * conf_file) {
    int i, j, number;
    char param_name[32]; /* used to generate variable parameter names */
    const char *str; /* used to store string value from JSON object */
    const char conf_obj_name[] = "SX130x_conf";
    JSON_Value *root_val = NULL;
    JSON_Value *val = NULL;
    JSON_Object *conf_obj = NULL;
    JSON_Object *conf_txgain_obj;
    JSON_Object *conf_ts_obj;
    JSON_Object *conf_sx1261_obj = NULL;
    JSON_Object *conf_scan_obj = NULL;
    JSON_Object *conf_lbt_obj = NULL;
    JSON_Object *conf_lbtchan_obj = NULL;
    JSON_Array *conf_txlut_array = NULL;
    JSON_Array *conf_lbtchan_array = NULL;
    JSON_Array *conf_demod_array = NULL;

    struct lgw_conf_board_s boardconf;
    struct lgw_conf_rxrf_s rfconf;
    struct lgw_conf_rxif_s ifconf;
    struct lgw_conf_demod_s demodconf;
    struct lgw_conf_ftime_s tsconf;
    struct lgw_conf_sx1261_s sx1261conf;
    uint32_t sf, bw, fdev;
    bool sx1250_tx_lut;
    size_t size;

    /* try to parse JSON */
    root_val = json_parse_file_with_comments(conf_file);
    if (root_val == NULL) {
        MSG("ERROR: %s is not a valid JSON file\n", conf_file);
        exit(EXIT_FAILURE);
    }

    /* point to the gateway configuration object */
    conf_obj = json_object_get_object(json_value_get_object(root_val), conf_obj_name);
    if (conf_obj == NULL) {
        MSG("INFO: %s does not contain a JSON object named %s\n", conf_file, conf_obj_name);
        return -1;
    } else {
        MSG("INFO: %s does contain a JSON object named %s, parsing SX1302 parameters\n", conf_file, conf_obj_name);
    }

    /* set board configuration */
    memset(&boardconf, 0, sizeof boardconf); /* initialize configuration structure */
    str = json_object_get_string(conf_obj, "com_type");
    if (str == NULL) {
        MSG("ERROR: com_type must be configured in %s\n", conf_file);
        return -1;
    } else if (!strncmp(str, "SPI", 3) || !strncmp(str, "spi", 3)) {
        boardconf.com_type = LGW_COM_SPI;
    } else if (!strncmp(str, "USB", 3) || !strncmp(str, "usb", 3)) {
        boardconf.com_type = LGW_COM_USB;
    } else {
        MSG("ERROR: invalid com type: %s (should be SPI or USB)\n", str);
        return -1;
    }
    com_type = boardconf.com_type;
    str = json_object_get_string(conf_obj, "com_path");
    if (str != NULL) {
        strncpy(boardconf.com_path, str, sizeof boardconf.com_path);
        boardconf.com_path[sizeof boardconf.com_path - 1] = '\0'; /* ensure string termination */
    } else {
        MSG("ERROR: com_path must be configured in %s\n", conf_file);
        return -1;
    }
    val = json_object_get_value(conf_obj, "lorawan_public"); /* fetch value (if possible) */
    if (json_value_get_type(val) == JSONBoolean) {
        boardconf.lorawan_public = (bool)json_value_get_boolean(val);
    } else {
        MSG("WARNING: Data type for lorawan_public seems wrong, please check\n");
        boardconf.lorawan_public = false;
    }
    val = json_object_get_value(conf_obj, "clksrc"); /* fetch value (if possible) */
    if (json_value_get_type(val) == JSONNumber) {
        boardconf.clksrc = (uint8_t)json_value_get_number(val);
    } else {
        MSG("WARNING: Data type for clksrc seems wrong, please check\n");
        boardconf.clksrc = 0;
    }
    val = json_object_get_value(conf_obj, "full_duplex"); /* fetch value (if possible) */
    if (json_value_get_type(val) == JSONBoolean) {
        boardconf.full_duplex = (bool)json_value_get_boolean(val);
    } else {
        MSG("WARNING: Data type for full_duplex seems wrong, please check\n");
        boardconf.full_duplex = false;
    }
    MSG("INFO: com_type %s, com_path %s, lorawan_public %d, clksrc %d, full_duplex %d\n", (boardconf.com_type == LGW_COM_SPI) ? "SPI" : "USB", boardconf.com_path, boardconf.lorawan_public, boardconf.clksrc, boardconf.full_duplex);
    /* all parameters parsed, submitting configuration to the HAL */
    if (lgw_board_setconf(&boardconf) != LGW_HAL_SUCCESS) {
        MSG("ERROR: Failed to configure board\n");
        return -1;
    }

    /* set antenna gain configuration */
    val = json_object_get_value(conf_obj, "antenna_gain"); /* fetch value (if possible) */
    if (val != NULL) {
        if (json_value_get_type(val) == JSONNumber) {
            antenna_gain = (int8_t)json_value_get_number(val);
        } else {
            MSG("WARNING: Data type for antenna_gain seems wrong, please check\n");
            antenna_gain = 0;
        }
    }
    MSG("INFO: antenna_gain %d dBi\n", antenna_gain);

    /* WM1303: configurable AGC periodic reload interval */
    {
        extern uint32_t agc_periodic_reload_interval_s;
        val = json_object_get_value(conf_obj, "agc_reload_interval_s");
        if (val != NULL && json_value_get_type(val) == JSONNumber) {
            agc_periodic_reload_interval_s = (uint32_t)json_value_get_number(val);
        }
        MSG("INFO: agc_reload_interval_s %u (0=disabled)\n", agc_periodic_reload_interval_s);
    }

    /* set timestamp configuration */
    conf_ts_obj = json_object_get_object(conf_obj, "fine_timestamp");
    if (conf_ts_obj == NULL) {
        MSG("INFO: %s does not contain a JSON object for fine timestamp\n", conf_file);
    } else {
        val = json_object_get_value(conf_ts_obj, "enable"); /* fetch value (if possible) */
        if (json_value_get_type(val) == JSONBoolean) {
            tsconf.enable = (bool)json_value_get_boolean(val);
        } else {
            MSG("WARNING: Data type for fine_timestamp.enable seems wrong, please check\n");
            tsconf.enable = false;
        }
        if (tsconf.enable == true) {
            str = json_object_get_string(conf_ts_obj, "mode");
            if (str == NULL) {
                MSG("ERROR: fine_timestamp.mode must be configured in %s\n", conf_file);
                return -1;
            } else if (!strncmp(str, "high_capacity", 13) || !strncmp(str, "HIGH_CAPACITY", 13)) {
                tsconf.mode = LGW_FTIME_MODE_HIGH_CAPACITY;
            } else if (!strncmp(str, "all_sf", 6) || !strncmp(str, "ALL_SF", 6)) {
                tsconf.mode = LGW_FTIME_MODE_ALL_SF;
            } else {
                MSG("ERROR: invalid fine timestamp mode: %s (should be high_capacity or all_sf)\n", str);
                return -1;
            }
            MSG("INFO: Configuring precision timestamp with %s mode\n", str);

            /* all parameters parsed, submitting configuration to the HAL */
            if (lgw_ftime_setconf(&tsconf) != LGW_HAL_SUCCESS) {
                MSG("ERROR: Failed to configure fine timestamp\n");
                return -1;
            }
        } else {
            MSG("INFO: Configuring legacy timestamp\n");
        }
    }

    /* set SX1261 configuration */
    memset(&sx1261conf, 0, sizeof sx1261conf); /* initialize configuration structure */
    conf_sx1261_obj = json_object_get_object(conf_obj, "sx1261_conf"); /* fetch value (if possible) */
    if (conf_sx1261_obj == NULL) {
        MSG("INFO: no configuration for SX1261\n");
    } else {
        /* Global SX1261 configuration */
        str = json_object_get_string(conf_sx1261_obj, "spi_path");
        if (str != NULL) {
            strncpy(sx1261conf.spi_path, str, sizeof sx1261conf.spi_path);
            sx1261conf.spi_path[sizeof sx1261conf.spi_path - 1] = '\0'; /* ensure string termination */
        } else {
            MSG("INFO: SX1261 spi_path is not configured in %s\n", conf_file);
        }
        val = json_object_get_value(conf_sx1261_obj, "rssi_offset"); /* fetch value (if possible) */
        if (json_value_get_type(val) == JSONNumber) {
            sx1261conf.rssi_offset = (int8_t)json_value_get_number(val);
        } else {
            MSG("WARNING: Data type for sx1261_conf.rssi_offset seems wrong, please check\n");
            sx1261conf.rssi_offset = 0;
        }

        /* Spectral Scan configuration */
        conf_scan_obj = json_object_get_object(conf_sx1261_obj, "spectral_scan"); /* fetch value (if possible) */
        if (conf_scan_obj == NULL) {
            MSG("INFO: no configuration for Spectral Scan\n");
        } else {
            val = json_object_get_value(conf_scan_obj, "enable"); /* fetch value (if possible) */
            if (json_value_get_type(val) == JSONBoolean) {
                /* Enable background spectral scan thread in packet forwarder */
                spectral_scan_params.enable = (bool)json_value_get_boolean(val);
            } else {
                MSG("WARNING: Data type for spectral_scan.enable seems wrong, please check\n");
            }
            if (spectral_scan_params.enable == true) {
                /* Enable the sx1261 radio hardware configuration to allow spectral scan */
                sx1261conf.enable = true;
                MSG("INFO: Spectral Scan with SX1261 is enabled\n");

                /* Get Spectral Scan Parameters */
                val = json_object_get_value(conf_scan_obj, "freq_start"); /* fetch value (if possible) */
                if (json_value_get_type(val) == JSONNumber) {
                    spectral_scan_params.freq_hz_start = (uint32_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for spectral_scan.freq_start seems wrong, please check\n");
                }
                val = json_object_get_value(conf_scan_obj, "nb_chan"); /* fetch value (if possible) */
                if (json_value_get_type(val) == JSONNumber) {
                    spectral_scan_params.nb_chan = (uint8_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for spectral_scan.nb_chan seems wrong, please check\n");
                }
                val = json_object_get_value(conf_scan_obj, "nb_scan"); /* fetch value (if possible) */
                if (json_value_get_type(val) == JSONNumber) {
                    spectral_scan_params.nb_scan = (uint16_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for spectral_scan.nb_scan seems wrong, please check\n");
                }
                val = json_object_get_value(conf_scan_obj, "pace_s"); /* fetch value (if possible) */
                if (json_value_get_type(val) == JSONNumber) {
                    spectral_scan_params.pace_s = (uint32_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for spectral_scan.pace_s seems wrong, please check\n");
                }
            }
        }

        /* LBT configuration */
        conf_lbt_obj = json_object_get_object(conf_sx1261_obj, "lbt"); /* fetch value (if possible) */
        if (conf_lbt_obj == NULL) {
            MSG("INFO: no configuration for LBT\n");
        } else {
            val = json_object_get_value(conf_lbt_obj, "enable"); /* fetch value (if possible) */
            if (json_value_get_type(val) == JSONBoolean) {
                sx1261conf.lbt_conf.enable = (bool)json_value_get_boolean(val);
            } else {
                MSG("WARNING: Data type for lbt.enable seems wrong, please check\n");
            }
            if (sx1261conf.lbt_conf.enable == true) {
                /* Enable the sx1261 radio hardware configuration to allow spectral scan */
                sx1261conf.enable = true;
                MSG("INFO: Listen-Before-Talk with SX1261 is enabled\n");

                val = json_object_get_value(conf_lbt_obj, "rssi_target"); /* fetch value (if possible) */
                if (json_value_get_type(val) == JSONNumber) {
                    sx1261conf.lbt_conf.rssi_target = (int8_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for lbt.rssi_target seems wrong, please check\n");
                    sx1261conf.lbt_conf.rssi_target = 0;
                }
                /* set LBT channels configuration */
                conf_lbtchan_array = json_object_get_array(conf_lbt_obj, "channels");
                if (conf_lbtchan_array != NULL) {
                    size_t lbt_channel_count = json_array_get_count(conf_lbtchan_array);
                    if (lbt_channel_count > LGW_LBT_CHANNEL_NB_MAX) {
                        MSG("ERROR: too many LBT channels (%zu, maximum %u)\n",
                            lbt_channel_count, (unsigned)LGW_LBT_CHANNEL_NB_MAX);
                        json_value_free(root_val);
                        return -1;
                    }
                    sx1261conf.lbt_conf.nb_channel = (uint8_t)lbt_channel_count;
                    MSG("INFO: %u LBT channels configured\n", sx1261conf.lbt_conf.nb_channel);
                }
                for (i = 0; i < (int)sx1261conf.lbt_conf.nb_channel; i++) {
                    /* Get LBT channel configuration object from array */
                    conf_lbtchan_obj = json_array_get_object(conf_lbtchan_array, i);

                    /* Per-channel LBT enable (default: true for backward compatibility) */
                    val = json_object_dotget_value(conf_lbtchan_obj, "enable");
                    if (val != NULL && json_value_get_type(val) == JSONBoolean) {
                        sx1261conf.lbt_conf.channels[i].enable = (bool)json_value_get_boolean(val);
                    } else {
                        sx1261conf.lbt_conf.channels[i].enable = true; /* default enabled */
                    }
                    MSG("INFO: sx1261_conf.lbt.channels[%d].enable:%s\n", i,
                        sx1261conf.lbt_conf.channels[i].enable ? "true" : "false");

                    /* Channel frequency */
                    val = json_object_dotget_value(conf_lbtchan_obj, "freq_hz"); /* fetch value (if possible) */
                    if (val != NULL) {
                        if (json_value_get_type(val) == JSONNumber) {
                            sx1261conf.lbt_conf.channels[i].freq_hz = (uint32_t)json_value_get_number(val);
                        } else {
                            MSG("WARNING: Data type for lbt.channels[%d].freq_hz seems wrong, please check\n", i);
                            sx1261conf.lbt_conf.channels[i].freq_hz = 0;
                        }
                    } else {
                        MSG("ERROR: no frequency defined for LBT channel %d\n", i);
                        return -1;
                    }

                    /* Channel bandiwdth */
                    val = json_object_dotget_value(conf_lbtchan_obj, "bandwidth"); /* fetch value (if possible) */
                    if (val != NULL) {
                        if (json_value_get_type(val) == JSONNumber) {
                            bw = (uint32_t)json_value_get_number(val);
                            switch(bw) {
                                case 500000: sx1261conf.lbt_conf.channels[i].bandwidth = BW_500KHZ; break;
                                case 250000: sx1261conf.lbt_conf.channels[i].bandwidth = BW_250KHZ; break;
                                case 125000: sx1261conf.lbt_conf.channels[i].bandwidth = BW_125KHZ; break;
                                case  62500: sx1261conf.lbt_conf.channels[i].bandwidth = BW_62K5HZ; break;
                                default: sx1261conf.lbt_conf.channels[i].bandwidth = BW_UNDEFINED;
                            }
                        } else {
                            MSG("WARNING: Data type for lbt.channels[%d].freq_hz seems wrong, please check\n", i);
                            sx1261conf.lbt_conf.channels[i].bandwidth = BW_UNDEFINED;
                        }
                    } else {
                        MSG("ERROR: no bandiwdth defined for LBT channel %d\n", i);
                        return -1;
                    }

                    /* Channel scan time */
                    val = json_object_dotget_value(conf_lbtchan_obj, "scan_time_us"); /* fetch value (if possible) */
                    if (val != NULL) {
                        if (json_value_get_type(val) == JSONNumber) {
                            if ((uint16_t)json_value_get_number(val) == 128) {
                                sx1261conf.lbt_conf.channels[i].scan_time_us = LGW_LBT_SCAN_TIME_128_US;
                            } else if ((uint16_t)json_value_get_number(val) == 5000) {
                                sx1261conf.lbt_conf.channels[i].scan_time_us = LGW_LBT_SCAN_TIME_5000_US;
                            } else {
                                MSG("ERROR: scan time not supported for LBT channel %d, must be 128 or 5000\n", i);
                                return -1;
                            }
                        } else {
                            MSG("WARNING: Data type for lbt.channels[%d].scan_time_us seems wrong, please check\n", i);
                            sx1261conf.lbt_conf.channels[i].scan_time_us = 0;
                        }
                    } else {
                        MSG("ERROR: no scan_time_us defined for LBT channel %d\n", i);
                        return -1;
                    }

                    /* Channel transmit time */
                    val = json_object_dotget_value(conf_lbtchan_obj, "transmit_time_ms"); /* fetch value (if possible) */
                    if (val != NULL) {
                        if (json_value_get_type(val) == JSONNumber) {
                            sx1261conf.lbt_conf.channels[i].transmit_time_ms = (uint16_t)json_value_get_number(val);
                        } else {
                            MSG("WARNING: Data type for lbt.channels[%d].transmit_time_ms seems wrong, please check\n", i);
                            sx1261conf.lbt_conf.channels[i].transmit_time_ms = 0;
                        }
                    } else {
                        MSG("ERROR: no transmit_time_ms defined for LBT channel %d\n", i);
                        return -1;
                    }

                    /* Per-channel RSSI target (optional, 0 = use global rssi_target) */
                    val = json_object_dotget_value(conf_lbtchan_obj, "rssi_target");
                    if (val != NULL && json_value_get_type(val) == JSONNumber) {
                        sx1261conf.lbt_conf.channels[i].rssi_target_dbm = (int8_t)json_value_get_number(val);
                        MSG("INFO: sx1261_conf.lbt.channels[%d].rssi_target:%d dBm\n", i, sx1261conf.lbt_conf.channels[i].rssi_target_dbm);
                    } else {
                        sx1261conf.lbt_conf.channels[i].rssi_target_dbm = 0;  /* use global */
                    }
                }

                /* --- WM1303: snapshot HAL LBT config for runtime lookup ---
                   Copy enabled flag, global rssi_target, and per-channel
                   rssi_target_dbm (resolved to global when set to 0) into the
                   hal_lbt_channels[] table consumed by hal_lbt_lookup(). */
                hal_lbt_enabled_global = sx1261conf.lbt_conf.enable;
                hal_lbt_global_target_dbm = sx1261conf.lbt_conf.rssi_target;
                hal_lbt_nb_channels = 0;
                for (i = 0; i < (int)sx1261conf.lbt_conf.nb_channel && i < HAL_LBT_MAX_CHANNELS; i++) {
                    hal_lbt_channels[i].freq_hz = sx1261conf.lbt_conf.channels[i].freq_hz;
                    hal_lbt_channels[i].bandwidth = sx1261conf.lbt_conf.channels[i].bandwidth;
                    hal_lbt_channels[i].enable = sx1261conf.lbt_conf.channels[i].enable;
                    int8_t per_ch = sx1261conf.lbt_conf.channels[i].rssi_target_dbm;
                    hal_lbt_channels[i].rssi_target_dbm = (per_ch != 0) ? per_ch : sx1261conf.lbt_conf.rssi_target;
                    hal_lbt_nb_channels++;
                }
                MSG("INFO: [HAL LBT snapshot] enabled=%s, global_thr=%d dBm, nb_channels=%d\n",
                    hal_lbt_enabled_global ? "true" : "false",
                    hal_lbt_global_target_dbm, hal_lbt_nb_channels);
                for (i = 0; i < hal_lbt_nb_channels; i++) {
                    MSG("INFO: [HAL LBT snapshot] ch%d: freq=%u Hz, thr=%d dBm, enable=%s\n",
                        i, hal_lbt_channels[i].freq_hz, hal_lbt_channels[i].rssi_target_dbm,
                        hal_lbt_channels[i].enable ? "true" : "false");
                }
            }
        }

        /* Custom pre-TX checks: CAD only (custom LBT has been removed —
           HAL LBT handles RSSI-based blocking inside lgw_send). The
           `custom_lbt` JSON object name is retained for backward config
           compatibility; only `cad_enable` and per-channel `cad_enabled`
           are honored. */
        JSON_Object *conf_custom_lbt_obj = json_object_get_object(conf_sx1261_obj, "custom_lbt");
        if (conf_custom_lbt_obj != NULL) {
            /* Read CAD enable flag */
            val = json_object_get_value(conf_custom_lbt_obj, "cad_enable");
            if (val != NULL && json_value_get_type(val) == JSONBoolean) {
                custom_cad_enable = (bool)json_value_get_boolean(val);
            }

            if (custom_cad_enable) {
                /* Enable the sx1261 radio hardware if not already enabled */
                sx1261conf.enable = true;

                /* Parse per-channel CAD config */
                JSON_Array *conf_clbt_channels = json_object_get_array(conf_custom_lbt_obj, "channels");
                if (conf_clbt_channels != NULL) {
                    custom_lbt_nb_channels = (int)json_array_get_count(conf_clbt_channels);
                    if (custom_lbt_nb_channels > CUSTOM_LBT_MAX_CHANNELS) {
                        MSG("WARNING: Pre-TX CAD has %d channels, truncating to %d\n",
                            custom_lbt_nb_channels, CUSTOM_LBT_MAX_CHANNELS);
                        custom_lbt_nb_channels = CUSTOM_LBT_MAX_CHANNELS;
                    }
                    for (i = 0; i < custom_lbt_nb_channels; i++) {
                        JSON_Object *ch_obj = json_array_get_object(conf_clbt_channels, i);
                        if (ch_obj != NULL) {
                            custom_lbt_channels[i].freq_hz = (uint32_t)json_object_get_number(ch_obj, "freq_hz");
                            val = json_object_get_value(ch_obj, "cad_enabled");
                            custom_lbt_channels[i].cad_enabled = (val != NULL && json_value_get_type(val) == JSONBoolean)
                                                                 ? (bool)json_value_get_boolean(val) : false;
                            MSG("INFO: Pre-TX channel %d: freq=%u Hz, cad=%s\n",
                                i, custom_lbt_channels[i].freq_hz,
                                custom_lbt_channels[i].cad_enabled ? "on" : "off");
                        }
                    }
                }

                MSG("INFO: Pre-TX CAD enabled (channels=%d)\n", custom_lbt_nb_channels);
            } else {
                MSG("INFO: Pre-TX CAD disabled\n");
            }
        } else {
            MSG("INFO: no configuration for pre-TX checks\n");
        }



        /* LoRa RX (Channel E) configuration */
        val = json_object_get_value(conf_sx1261_obj, "lora_rx");
        if (json_value_get_type(val) == JSONObject) {
            JSON_Object *lora_rx_obj = json_value_get_object(val);
            val = json_object_get_value(lora_rx_obj, "enable");
            if (json_value_get_type(val) == JSONBoolean) {
                sx1261conf.lora_rx_enable = (bool)json_value_get_boolean(val);
            } else {
                MSG("WARNING: Data type for lora_rx.enable seems wrong, please check\n");
            }
            if (sx1261conf.lora_rx_enable == true) {
                /* Enable the sx1261 radio hardware if not already enabled */
                sx1261conf.enable = true;
                MSG("INFO: LoRa RX (Channel E) on SX1261 is enabled\n");

                val = json_object_get_value(lora_rx_obj, "freq_hz");
                if (json_value_get_type(val) == JSONNumber) {
                    sx1261conf.lora_rx_freq = (uint32_t)json_value_get_number(val);
                } else {
                    MSG("WARNING: Data type for lora_rx.freq_hz seems wrong, please check\n");
                }

                val = json_object_get_value(lora_rx_obj, "bandwidth");
                if (json_value_get_type(val) == JSONNumber) {
                    bw = (uint32_t)json_value_get_number(val);
                    switch(bw) {
                        case 500000: sx1261conf.lora_rx_bw = BW_500KHZ; break;
                        case 250000: sx1261conf.lora_rx_bw = BW_250KHZ; break;
                        case 125000: sx1261conf.lora_rx_bw = BW_125KHZ; break;
                        case 62500:  sx1261conf.lora_rx_bw = BW_62K5HZ; break;
                        default:
                            MSG("WARNING: unsupported lora_rx bandwidth %u, using 62500\n", bw);
                            sx1261conf.lora_rx_bw = BW_62K5HZ;
                    }
                } else {
                    MSG("WARNING: Data type for lora_rx.bandwidth seems wrong, defaulting to 62500\n");
                    sx1261conf.lora_rx_bw = BW_62K5HZ;
                }

                val = json_object_get_value(lora_rx_obj, "spreading_factor");
                if (json_value_get_type(val) == JSONNumber) {
                    sx1261conf.lora_rx_sf = (uint8_t)json_value_get_number(val);
                    if (sx1261conf.lora_rx_sf < 7 || sx1261conf.lora_rx_sf > 12) {
                        MSG("WARNING: lora_rx.spreading_factor %u out of range, using 8\n", sx1261conf.lora_rx_sf);
                        sx1261conf.lora_rx_sf = 8;
                    }
                } else {
                    MSG("WARNING: Data type for lora_rx.spreading_factor seems wrong, defaulting to 8\n");
                    sx1261conf.lora_rx_sf = 8;
                }

                val = json_object_get_value(lora_rx_obj, "coding_rate");
                if (json_value_get_type(val) == JSONNumber) {
                    sx1261conf.lora_rx_cr = (uint8_t)json_value_get_number(val);
                    if (sx1261conf.lora_rx_cr < 1 || sx1261conf.lora_rx_cr > 4) {
                        MSG("WARNING: lora_rx.coding_rate %u out of range, using 1 (4/5)\n", sx1261conf.lora_rx_cr);
                        sx1261conf.lora_rx_cr = 1;
                    }
                } else {
                    MSG("WARNING: Data type for lora_rx.coding_rate seems wrong, defaulting to 1 (4/5)\n");
                    sx1261conf.lora_rx_cr = 1;
                }

                /* Parse boosted LNA RX gain (optional, default: true) */
                val = json_object_get_value(lora_rx_obj, "boosted");
                if (json_value_get_type(val) == JSONBoolean) {
                    sx1261conf.lora_rx_boosted = (bool)json_value_get_boolean(val);
                } else {
                    sx1261conf.lora_rx_boosted = true; /* default: boosted for max sensitivity */
                }

                MSG("INFO: LoRa RX Channel E: freq=%uHz, BW=0x%02X, SF%u, CR%u, boosted=%s\n",
                    sx1261conf.lora_rx_freq, sx1261conf.lora_rx_bw,
                    sx1261conf.lora_rx_sf, sx1261conf.lora_rx_cr,
                    sx1261conf.lora_rx_boosted ? "true" : "false");
            }
        }

        /* all parameters parsed, submitting configuration to the HAL */
        if (lgw_sx1261_setconf(&sx1261conf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: Failed to configure the SX1261 radio\n");
            return -1;
        }
    }

    /* set configuration for RF chains */
    for (i = 0; i < LGW_RF_CHAIN_NB; ++i) {
        memset(&rfconf, 0, sizeof rfconf); /* initialize configuration structure */
        snprintf(param_name, sizeof param_name, "radio_%i", i); /* compose parameter path inside JSON structure */
        val = json_object_get_value(conf_obj, param_name); /* fetch value (if possible) */
        if (json_value_get_type(val) != JSONObject) {
            MSG("INFO: no configuration for radio %i\n", i);
            continue;
        }
        /* there is an object to configure that radio, let's parse it */
        snprintf(param_name, sizeof param_name, "radio_%i.enable", i);
        val = json_object_dotget_value(conf_obj, param_name);
        if (json_value_get_type(val) == JSONBoolean) {
            rfconf.enable = (bool)json_value_get_boolean(val);
        } else {
            rfconf.enable = false;
        }
        if (rfconf.enable == false) { /* radio disabled, nothing else to parse */
            MSG("INFO: radio %i disabled\n", i);
        } else  { /* radio enabled, will parse the other parameters */
            snprintf(param_name, sizeof param_name, "radio_%i.freq", i);
            rfconf.freq_hz = (uint32_t)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_offset", i);
            rfconf.rssi_offset = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_tcomp.coeff_a", i);
            rfconf.rssi_tcomp.coeff_a = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_tcomp.coeff_b", i);
            rfconf.rssi_tcomp.coeff_b = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_tcomp.coeff_c", i);
            rfconf.rssi_tcomp.coeff_c = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_tcomp.coeff_d", i);
            rfconf.rssi_tcomp.coeff_d = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.rssi_tcomp.coeff_e", i);
            rfconf.rssi_tcomp.coeff_e = (float)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "radio_%i.type", i);
            str = json_object_dotget_string(conf_obj, param_name);
            if (!strncmp(str, "SX1255", 6)) {
                rfconf.type = LGW_RADIO_TYPE_SX1255;
            } else if (!strncmp(str, "SX1257", 6)) {
                rfconf.type = LGW_RADIO_TYPE_SX1257;
            } else if (!strncmp(str, "SX1250", 6)) {
                rfconf.type = LGW_RADIO_TYPE_SX1250;
            } else {
                MSG("WARNING: invalid radio type: %s (should be SX1255 or SX1257 or SX1250)\n", str);
            }
            snprintf(param_name, sizeof param_name, "radio_%i.single_input_mode", i);
            val = json_object_dotget_value(conf_obj, param_name);
            if (json_value_get_type(val) == JSONBoolean) {
                rfconf.single_input_mode = (bool)json_value_get_boolean(val);
            } else {
                rfconf.single_input_mode = false;
            }

            snprintf(param_name, sizeof param_name, "radio_%i.tx_enable", i);
            val = json_object_dotget_value(conf_obj, param_name);
            if (json_value_get_type(val) == JSONBoolean) {
                rfconf.tx_enable = (bool)json_value_get_boolean(val);
                tx_enable[i] = rfconf.tx_enable; /* update global context for later check */
                if (rfconf.tx_enable == true) {
                    /* tx is enabled on this rf chain, we need its frequency range */
                    snprintf(param_name, sizeof param_name, "radio_%i.tx_freq_min", i);
                    tx_freq_min[i] = (uint32_t)json_object_dotget_number(conf_obj, param_name);
                    snprintf(param_name, sizeof param_name, "radio_%i.tx_freq_max", i);
                    tx_freq_max[i] = (uint32_t)json_object_dotget_number(conf_obj, param_name);
                    if ((tx_freq_min[i] == 0) || (tx_freq_max[i] == 0)) {
                        MSG("WARNING: no frequency range specified for TX rf chain %d\n", i);
                    }

                    /* set configuration for tx gains */
                    memset(&txlut[i], 0, sizeof txlut[i]); /* initialize configuration structure */
                    snprintf(param_name, sizeof param_name, "radio_%i.tx_gain_lut", i);
                    conf_txlut_array = json_object_dotget_array(conf_obj, param_name);
                    if (conf_txlut_array != NULL) {
                        txlut[i].size = json_array_get_count(conf_txlut_array);
                        /* Detect if we have a sx125x or sx1250 configuration */
                        conf_txgain_obj = json_array_get_object(conf_txlut_array, 0);
                        val = json_object_dotget_value(conf_txgain_obj, "pwr_idx");
                        if (val != NULL) {
                            printf("INFO: Configuring Tx Gain LUT for rf_chain %d with %u indexes for sx1250\n", i, txlut[i].size);
                            sx1250_tx_lut = true;
                        } else {
                            printf("INFO: Configuring Tx Gain LUT for rf_chain %d with %u indexes for sx125x\n", i, txlut[i].size);
                            sx1250_tx_lut = false;
                        }
                        /* Parse the table */
                        for (j = 0; j < (int)txlut[i].size; j++) {
                             /* Sanity check */
                            if (j >= TX_GAIN_LUT_SIZE_MAX) {
                                printf("ERROR: TX Gain LUT [%d] index %d not supported, skip it\n", i, j);
                                break;
                            }
                            /* Get TX gain object from LUT */
                            conf_txgain_obj = json_array_get_object(conf_txlut_array, j);
                            /* rf power */
                            val = json_object_dotget_value(conf_txgain_obj, "rf_power");
                            if (json_value_get_type(val) == JSONNumber) {
                                txlut[i].lut[j].rf_power = (int8_t)json_value_get_number(val);
                            } else {
                                printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "rf_power", j);
                                txlut[i].lut[j].rf_power = 0;
                            }
                            /* PA gain */
                            val = json_object_dotget_value(conf_txgain_obj, "pa_gain");
                            if (json_value_get_type(val) == JSONNumber) {
                                txlut[i].lut[j].pa_gain = (uint8_t)json_value_get_number(val);
                            } else {
                                printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "pa_gain", j);
                                txlut[i].lut[j].pa_gain = 0;
                            }
                            if (sx1250_tx_lut == false) {
                                /* DIG gain */
                                val = json_object_dotget_value(conf_txgain_obj, "dig_gain");
                                if (json_value_get_type(val) == JSONNumber) {
                                    txlut[i].lut[j].dig_gain = (uint8_t)json_value_get_number(val);
                                } else {
                                    printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "dig_gain", j);
                                    txlut[i].lut[j].dig_gain = 0;
                                }
                                /* DAC gain */
                                val = json_object_dotget_value(conf_txgain_obj, "dac_gain");
                                if (json_value_get_type(val) == JSONNumber) {
                                    txlut[i].lut[j].dac_gain = (uint8_t)json_value_get_number(val);
                                } else {
                                    printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "dac_gain", j);
                                    txlut[i].lut[j].dac_gain = 3; /* This is the only dac_gain supported for now */
                                }
                                /* MIX gain */
                                val = json_object_dotget_value(conf_txgain_obj, "mix_gain");
                                if (json_value_get_type(val) == JSONNumber) {
                                    txlut[i].lut[j].mix_gain = (uint8_t)json_value_get_number(val);
                                } else {
                                    printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "mix_gain", j);
                                    txlut[i].lut[j].mix_gain = 0;
                                }
                            } else {
                                /* TODO: rework this, should not be needed for sx1250 */
                                txlut[i].lut[j].mix_gain = 5;

                                /* power index */
                                val = json_object_dotget_value(conf_txgain_obj, "pwr_idx");
                                if (json_value_get_type(val) == JSONNumber) {
                                    txlut[i].lut[j].pwr_idx = (uint8_t)json_value_get_number(val);
                                } else {
                                    printf("WARNING: Data type for %s[%d] seems wrong, please check\n", "pwr_idx", j);
                                    txlut[i].lut[j].pwr_idx = 0;
                                }
                            }
                        }
                        /* all parameters parsed, submitting configuration to the HAL */
                        if (txlut[i].size > 0) {
                            if (lgw_txgain_setconf(i, &txlut[i]) != LGW_HAL_SUCCESS) {
                                MSG("ERROR: Failed to configure concentrator TX Gain LUT for rf_chain %d\n", i);
                                return -1;
                            }
                        } else {
                            MSG("WARNING: No TX gain LUT defined for rf_chain %d\n", i);
                        }
                    } else {
                        MSG("WARNING: No TX gain LUT defined for rf_chain %d\n", i);
                    }
                }
            } else {
                rfconf.tx_enable = false;
            }
            MSG("INFO: radio %i enabled (type %s), center frequency %u, RSSI offset %f, tx enabled %d, single input mode %d\n", i, str, rfconf.freq_hz, rfconf.rssi_offset, rfconf.tx_enable, rfconf.single_input_mode);
        }
        /* all parameters parsed, submitting configuration to the HAL */
        if (lgw_rxrf_setconf(i, &rfconf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: invalid configuration for radio %i\n", i);
            return -1;
        }
    }

    /* set configuration for demodulators */
    memset(&demodconf, 0, sizeof demodconf); /* initialize configuration structure */
    val = json_object_get_value(conf_obj, "chan_multiSF_All"); /* fetch value (if possible) */
    if (json_value_get_type(val) != JSONObject) {
        MSG("INFO: no configuration for LoRa multi-SF spreading factors enabling\n");
    } else {
        conf_demod_array = json_object_dotget_array(conf_obj, "chan_multiSF_All.spreading_factor_enable");
        if ((conf_demod_array != NULL) && ((size = json_array_get_count(conf_demod_array)) <= LGW_MULTI_NB)) {
            for (i = 0; i < (int)size; i++) {
                number = json_array_get_number(conf_demod_array, i);
                if (number < 5 || number > 12) {
                    MSG("WARNING: failed to parse chan_multiSF_All.spreading_factor_enable (wrong value at idx %d)\n", i);
                    demodconf.multisf_datarate = 0xFF; /* enable all SFs */
                    break;
                } else {
                    /* set corresponding bit in the bitmask SF5 is LSB -> SF12 is MSB */
                    demodconf.multisf_datarate |= (1 << (number - 5));
                }
            }
        } else {
            MSG("WARNING: failed to parse chan_multiSF_All.spreading_factor_enable\n");
            demodconf.multisf_datarate = 0xFF; /* enable all SFs */
        }
        /* all parameters parsed, submitting configuration to the HAL */
        if (lgw_demod_setconf(&demodconf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: invalid configuration for demodulation parameters\n");
            return -1;
        }
    }

    /* set configuration for Lora multi-SF channels (bandwidth cannot be set) */
    for (i = 0; i < LGW_MULTI_NB; ++i) {
        memset(&ifconf, 0, sizeof ifconf); /* initialize configuration structure */
        snprintf(param_name, sizeof param_name, "chan_multiSF_%i", i); /* compose parameter path inside JSON structure */
        val = json_object_get_value(conf_obj, param_name); /* fetch value (if possible) */
        if (json_value_get_type(val) != JSONObject) {
            MSG("INFO: no configuration for Lora multi-SF channel %i\n", i);
            continue;
        }
        /* there is an object to configure that Lora multi-SF channel, let's parse it */
        snprintf(param_name, sizeof param_name, "chan_multiSF_%i.enable", i);
        val = json_object_dotget_value(conf_obj, param_name);
        if (json_value_get_type(val) == JSONBoolean) {
            ifconf.enable = (bool)json_value_get_boolean(val);
        } else {
            ifconf.enable = false;
        }
        if (ifconf.enable == false) { /* Lora multi-SF channel disabled, nothing else to parse */
            MSG("INFO: Lora multi-SF channel %i disabled\n", i);
        } else  { /* Lora multi-SF channel enabled, will parse the other parameters */
            snprintf(param_name, sizeof param_name, "chan_multiSF_%i.radio", i);
            ifconf.rf_chain = (uint32_t)json_object_dotget_number(conf_obj, param_name);
            snprintf(param_name, sizeof param_name, "chan_multiSF_%i.if", i);
            ifconf.freq_hz = (int32_t)json_object_dotget_number(conf_obj, param_name);
            // TODO: handle individual SF enabling and disabling (spread_factor)
            MSG("INFO: Lora multi-SF channel %i>  radio %i, IF %i Hz, 125 kHz bw, SF 5 to 12\n", i, ifconf.rf_chain, ifconf.freq_hz);
        }
        /* all parameters parsed, submitting configuration to the HAL */
        if (lgw_rxif_setconf(i, &ifconf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: invalid configuration for Lora multi-SF channel %i\n", i);
            return -1;
        }
    }

    /* set configuration for Lora standard channel */
    memset(&ifconf, 0, sizeof ifconf); /* initialize configuration structure */
    val = json_object_get_value(conf_obj, "chan_Lora_std"); /* fetch value (if possible) */
    if (json_value_get_type(val) != JSONObject) {
        MSG("INFO: no configuration for Lora standard channel\n");
    } else {
        val = json_object_dotget_value(conf_obj, "chan_Lora_std.enable");
        if (json_value_get_type(val) == JSONBoolean) {
            ifconf.enable = (bool)json_value_get_boolean(val);
        } else {
            ifconf.enable = false;
        }
        if (ifconf.enable == false) {
            MSG("INFO: Lora standard channel %i disabled\n", i);
        } else  {
            ifconf.rf_chain = (uint32_t)json_object_dotget_number(conf_obj, "chan_Lora_std.radio");
            ifconf.freq_hz = (int32_t)json_object_dotget_number(conf_obj, "chan_Lora_std.if");
            bw = (uint32_t)json_object_dotget_number(conf_obj, "chan_Lora_std.bandwidth");
            switch(bw) {
                case 500000: ifconf.bandwidth = BW_500KHZ; break;
                case 250000: ifconf.bandwidth = BW_250KHZ; break;
                case 125000: ifconf.bandwidth = BW_125KHZ; break;
                default: ifconf.bandwidth = BW_UNDEFINED;
            }
            sf = (uint32_t)json_object_dotget_number(conf_obj, "chan_Lora_std.spread_factor");
            switch(sf) {
                case  5: ifconf.datarate = DR_LORA_SF5;  break;
                case  6: ifconf.datarate = DR_LORA_SF6;  break;
                case  7: ifconf.datarate = DR_LORA_SF7;  break;
                case  8: ifconf.datarate = DR_LORA_SF8;  break;
                case  9: ifconf.datarate = DR_LORA_SF9;  break;
                case 10: ifconf.datarate = DR_LORA_SF10; break;
                case 11: ifconf.datarate = DR_LORA_SF11; break;
                case 12: ifconf.datarate = DR_LORA_SF12; break;
                default: ifconf.datarate = DR_UNDEFINED;
            }
            val = json_object_dotget_value(conf_obj, "chan_Lora_std.implicit_hdr");
            if (json_value_get_type(val) == JSONBoolean) {
                ifconf.implicit_hdr = (bool)json_value_get_boolean(val);
            } else {
                ifconf.implicit_hdr = false;
            }
            if (ifconf.implicit_hdr == true) {
                val = json_object_dotget_value(conf_obj, "chan_Lora_std.implicit_payload_length");
                if (json_value_get_type(val) == JSONNumber) {
                    ifconf.implicit_payload_length = (uint8_t)json_value_get_number(val);
                } else {
                    MSG("ERROR: payload length setting is mandatory for implicit header mode\n");
                    return -1;
                }
                val = json_object_dotget_value(conf_obj, "chan_Lora_std.implicit_crc_en");
                if (json_value_get_type(val) == JSONBoolean) {
                    ifconf.implicit_crc_en = (bool)json_value_get_boolean(val);
                } else {
                    MSG("ERROR: CRC enable setting is mandatory for implicit header mode\n");
                    return -1;
                }
                val = json_object_dotget_value(conf_obj, "chan_Lora_std.implicit_coderate");
                if (json_value_get_type(val) == JSONNumber) {
                    ifconf.implicit_coderate = (uint8_t)json_value_get_number(val);
                } else {
                    MSG("ERROR: coding rate setting is mandatory for implicit header mode\n");
                    return -1;
                }
            }

            MSG("INFO: Lora std channel> radio %i, IF %i Hz, %u Hz bw, SF %u, %s\n", ifconf.rf_chain, ifconf.freq_hz, bw, sf, (ifconf.implicit_hdr == true) ? "Implicit header" : "Explicit header");
        }
        if (lgw_rxif_setconf(8, &ifconf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: invalid configuration for Lora standard channel\n");
            return -1;
        }
    }

    /* set configuration for FSK channel */
    memset(&ifconf, 0, sizeof ifconf); /* initialize configuration structure */
    val = json_object_get_value(conf_obj, "chan_FSK"); /* fetch value (if possible) */
    if (json_value_get_type(val) != JSONObject) {
        MSG("INFO: no configuration for FSK channel\n");
    } else {
        val = json_object_dotget_value(conf_obj, "chan_FSK.enable");
        if (json_value_get_type(val) == JSONBoolean) {
            ifconf.enable = (bool)json_value_get_boolean(val);
        } else {
            ifconf.enable = false;
        }
        if (ifconf.enable == false) {
            MSG("INFO: FSK channel %i disabled\n", i);
        } else  {
            ifconf.rf_chain = (uint32_t)json_object_dotget_number(conf_obj, "chan_FSK.radio");
            ifconf.freq_hz = (int32_t)json_object_dotget_number(conf_obj, "chan_FSK.if");
            bw = (uint32_t)json_object_dotget_number(conf_obj, "chan_FSK.bandwidth");
            fdev = (uint32_t)json_object_dotget_number(conf_obj, "chan_FSK.freq_deviation");
            ifconf.datarate = (uint32_t)json_object_dotget_number(conf_obj, "chan_FSK.datarate");

            /* if chan_FSK.bandwidth is set, it has priority over chan_FSK.freq_deviation */
            if ((bw == 0) && (fdev != 0)) {
                bw = 2 * fdev + ifconf.datarate;
            }
            if      (bw == 0)      ifconf.bandwidth = BW_UNDEFINED;
#if 0 /* TODO */
            else if (bw <= 7800)   ifconf.bandwidth = BW_7K8HZ;
            else if (bw <= 15600)  ifconf.bandwidth = BW_15K6HZ;
            else if (bw <= 31200)  ifconf.bandwidth = BW_31K2HZ;
            else if (bw <= 62500)  ifconf.bandwidth = BW_62K5HZ;
#endif
            else if (bw <= 125000) ifconf.bandwidth = BW_125KHZ;
            else if (bw <= 250000) ifconf.bandwidth = BW_250KHZ;
            else if (bw <= 500000) ifconf.bandwidth = BW_500KHZ;
            else ifconf.bandwidth = BW_UNDEFINED;

            MSG("INFO: FSK channel> radio %i, IF %i Hz, %u Hz bw, %u bps datarate\n", ifconf.rf_chain, ifconf.freq_hz, bw, ifconf.datarate);
        }
        if (lgw_rxif_setconf(9, &ifconf) != LGW_HAL_SUCCESS) {
            MSG("ERROR: invalid configuration for FSK channel\n");
            return -1;
        }
    }

    /* Parse CAPTURE_RAM streaming configuration */
    capture_conf_parse(json_value_get_object(root_val));
    json_value_free(root_val);

    return 0;
}

static int parse_gateway_configuration(const char * conf_file) {
    const char conf_obj_name[] = "gateway_conf";
    JSON_Value *root_val;
    JSON_Object *conf_obj = NULL;
    JSON_Value *val = NULL; /* needed to detect the absence of some fields */
    const char *str; /* pointer to sub-strings in the JSON data */
    unsigned long long ull = 0;

    /* try to parse JSON */
    root_val = json_parse_file_with_comments(conf_file);
    if (root_val == NULL) {
        MSG("ERROR: %s is not a valid JSON file\n", conf_file);
        exit(EXIT_FAILURE);
    }

    /* point to the gateway configuration object */
    conf_obj = json_object_get_object(json_value_get_object(root_val), conf_obj_name);
    if (conf_obj == NULL) {
        MSG("INFO: %s does not contain a JSON object named %s\n", conf_file, conf_obj_name);
        return -1;
    } else {
        MSG("INFO: %s does contain a JSON object named %s, parsing gateway parameters\n", conf_file, conf_obj_name);
    }

    /* gateway unique identifier (aka MAC address) (optional) */
    str = json_object_get_string(conf_obj, "gateway_ID");
    if (str != NULL) {
        sscanf(str, "%llx", &ull);
        lgwm = ull;
        MSG("INFO: gateway MAC address is configured to %016llX\n", ull);
    }

    /* server hostname or IP address (optional) */
    str = json_object_get_string(conf_obj, "server_address");
    if (str != NULL) {
        strncpy(serv_addr, str, sizeof serv_addr);
        serv_addr[sizeof serv_addr - 1] = '\0'; /* ensure string termination */
        MSG("INFO: server hostname or IP address is configured to \"%s\"\n", serv_addr);
    }

    /* get up and down ports (optional) */
    val = json_object_get_value(conf_obj, "serv_port_up");
    if (val != NULL) {
        snprintf(serv_port_up, sizeof serv_port_up, "%u", (uint16_t)json_value_get_number(val));
        MSG("INFO: upstream port is configured to \"%s\"\n", serv_port_up);
    }
    val = json_object_get_value(conf_obj, "serv_port_down");
    if (val != NULL) {
        snprintf(serv_port_down, sizeof serv_port_down, "%u", (uint16_t)json_value_get_number(val));
        MSG("INFO: downstream port is configured to \"%s\"\n", serv_port_down);
    }

    /* get keep-alive interval (in seconds) for downstream (optional) */
    val = json_object_get_value(conf_obj, "keepalive_interval");
    if (val != NULL) {
        keepalive_time = (int)json_value_get_number(val);
        MSG("INFO: downstream keep-alive interval is configured to %d seconds\n", keepalive_time);
    }

    /* get interval (in seconds) for statistics display (optional) */
    val = json_object_get_value(conf_obj, "stat_interval");
    if (val != NULL) {
        stat_interval = (unsigned)json_value_get_number(val);
        MSG("INFO: statistics display interval is configured to %u seconds\n", stat_interval);
    }

    /* get time-out value (in ms) for upstream datagrams (optional) */
    val = json_object_get_value(conf_obj, "push_timeout_ms");
    if (val != NULL) {
        push_timeout_half.tv_usec = 500 * (long int)json_value_get_number(val);
        MSG("INFO: upstream PUSH_DATA time-out is configured to %u ms\n", (unsigned)(push_timeout_half.tv_usec / 500));
    }

    /* packet filtering parameters */
    val = json_object_get_value(conf_obj, "forward_crc_valid");
    if (json_value_get_type(val) == JSONBoolean) {
        fwd_valid_pkt = (bool)json_value_get_boolean(val);
    }
    MSG("INFO: packets received with a valid CRC will%s be forwarded\n", (fwd_valid_pkt ? "" : " NOT"));
    val = json_object_get_value(conf_obj, "forward_crc_error");
    if (json_value_get_type(val) == JSONBoolean) {
        fwd_error_pkt = (bool)json_value_get_boolean(val);
    }
    MSG("INFO: packets received with a CRC error will%s be forwarded\n", (fwd_error_pkt ? "" : " NOT"));
    val = json_object_get_value(conf_obj, "forward_crc_disabled");
    if (json_value_get_type(val) == JSONBoolean) {
        fwd_nocrc_pkt = (bool)json_value_get_boolean(val);
    }
    MSG("INFO: packets received with no CRC will%s be forwarded\n", (fwd_nocrc_pkt ? "" : " NOT"));

    /* GPS module TTY path (optional) */
    str = json_object_get_string(conf_obj, "gps_tty_path");
    if (str != NULL) {
        strncpy(gps_tty_path, str, sizeof gps_tty_path);
        gps_tty_path[sizeof gps_tty_path - 1] = '\0'; /* ensure string termination */
        MSG("INFO: GPS serial port path is configured to \"%s\"\n", gps_tty_path);
    }

    /* get reference coordinates */
    val = json_object_get_value(conf_obj, "ref_latitude");
    if (val != NULL) {
        reference_coord.lat = (double)json_value_get_number(val);
        MSG("INFO: Reference latitude is configured to %f deg\n", reference_coord.lat);
    }
    val = json_object_get_value(conf_obj, "ref_longitude");
    if (val != NULL) {
        reference_coord.lon = (double)json_value_get_number(val);
        MSG("INFO: Reference longitude is configured to %f deg\n", reference_coord.lon);
    }
    val = json_object_get_value(conf_obj, "ref_altitude");
    if (val != NULL) {
        reference_coord.alt = (short)json_value_get_number(val);
        MSG("INFO: Reference altitude is configured to %i meters\n", reference_coord.alt);
    }

    /* Gateway GPS coordinates hardcoding (aka. faking) option */
    val = json_object_get_value(conf_obj, "fake_gps");
    if (json_value_get_type(val) == JSONBoolean) {
        gps_fake_enable = (bool)json_value_get_boolean(val);
        if (gps_fake_enable == true) {
            MSG("INFO: fake GPS is enabled\n");
        } else {
            MSG("INFO: fake GPS is disabled\n");
        }
    }

    /* Beacon signal period (optional) */
    val = json_object_get_value(conf_obj, "beacon_period");
    if (val != NULL) {
        beacon_period = (uint32_t)json_value_get_number(val);
        if ((beacon_period > 0) && (beacon_period < 6)) {
            MSG("ERROR: invalid configuration for Beacon period, must be >= 6s\n");
            return -1;
        } else {
            MSG("INFO: Beaconing period is configured to %u seconds\n", beacon_period);
        }
    }

    /* Beacon TX frequency (optional) */
    val = json_object_get_value(conf_obj, "beacon_freq_hz");
    if (val != NULL) {
        beacon_freq_hz = (uint32_t)json_value_get_number(val);
        MSG("INFO: Beaconing signal will be emitted at %u Hz\n", beacon_freq_hz);
    }

    /* Number of beacon channels (optional) */
    val = json_object_get_value(conf_obj, "beacon_freq_nb");
    if (val != NULL) {
        beacon_freq_nb = (uint8_t)json_value_get_number(val);
        MSG("INFO: Beaconing channel number is set to %u\n", beacon_freq_nb);
    }

    /* Frequency step between beacon channels (optional) */
    val = json_object_get_value(conf_obj, "beacon_freq_step");
    if (val != NULL) {
        beacon_freq_step = (uint32_t)json_value_get_number(val);
        MSG("INFO: Beaconing channel frequency step is set to %uHz\n", beacon_freq_step);
    }

    /* Beacon datarate (optional) */
    val = json_object_get_value(conf_obj, "beacon_datarate");
    if (val != NULL) {
        beacon_datarate = (uint8_t)json_value_get_number(val);
        MSG("INFO: Beaconing datarate is set to SF%d\n", beacon_datarate);
    }

    /* Beacon modulation bandwidth (optional) */
    val = json_object_get_value(conf_obj, "beacon_bw_hz");
    if (val != NULL) {
        beacon_bw_hz = (uint32_t)json_value_get_number(val);
        MSG("INFO: Beaconing modulation bandwidth is set to %uHz\n", beacon_bw_hz);
    }

    /* Beacon TX power (optional) */
    val = json_object_get_value(conf_obj, "beacon_power");
    if (val != NULL) {
        beacon_power = (int8_t)json_value_get_number(val);
        MSG("INFO: Beaconing TX power is set to %ddBm\n", beacon_power);
    }

    /* Beacon information descriptor (optional) */
    val = json_object_get_value(conf_obj, "beacon_infodesc");
    if (val != NULL) {
        beacon_infodesc = (uint8_t)json_value_get_number(val);
        MSG("INFO: Beaconing information descriptor is set to %u\n", beacon_infodesc);
    }

    /* Auto-quit threshold (optional) */
    val = json_object_get_value(conf_obj, "autoquit_threshold");
    if (val != NULL) {
        autoquit_threshold = (uint32_t)json_value_get_number(val);
        MSG("INFO: Auto-quit after %u non-acknowledged PULL_DATA\n", autoquit_threshold);
    }

    /* free JSON parsing data structure */
    json_value_free(root_val);
    return 0;
}

static int parse_debug_configuration(const char * conf_file) {
    int i;
    const char conf_obj_name[] = "debug_conf";
    JSON_Value *root_val;
    JSON_Object *conf_obj = NULL;
    JSON_Array *conf_array = NULL;
    JSON_Object *conf_obj_array = NULL;
    const char *str; /* pointer to sub-strings in the JSON data */

    /* Initialize structure */
    memset(&debugconf, 0, sizeof debugconf);

    /* try to parse JSON */
    root_val = json_parse_file_with_comments(conf_file);
    if (root_val == NULL) {
        MSG("ERROR: %s is not a valid JSON file\n", conf_file);
        exit(EXIT_FAILURE);
    }

    /* point to the gateway configuration object */
    conf_obj = json_object_get_object(json_value_get_object(root_val), conf_obj_name);
    if (conf_obj == NULL) {
        MSG("INFO: %s does not contain a JSON object named %s\n", conf_file, conf_obj_name);
        json_value_free(root_val);
        return -1;
    } else {
        MSG("INFO: %s does contain a JSON object named %s, parsing debug parameters\n", conf_file, conf_obj_name);
    }

    /* Get reference payload configuration */
    conf_array = json_object_get_array (conf_obj, "ref_payload");
    if (conf_array != NULL) {
        size_t ref_payload_count = json_array_get_count(conf_array);
        if (ref_payload_count > ARRAY_SIZE(debugconf.ref_payload)) {
            MSG("ERROR: too many debug reference payloads (%zu, maximum %zu)\n",
                ref_payload_count, ARRAY_SIZE(debugconf.ref_payload));
            json_value_free(root_val);
            return -1;
        }
        debugconf.nb_ref_payload = (uint8_t)ref_payload_count;
        MSG("INFO: got %u debug reference payload\n", debugconf.nb_ref_payload);

        for (i = 0; i < (int)debugconf.nb_ref_payload; i++) {
            conf_obj_array = json_array_get_object(conf_array, i);
            /* id */
            str = json_object_get_string(conf_obj_array, "id");
            if (str != NULL) {
                sscanf(str, "0x%08X", &(debugconf.ref_payload[i].id));
                MSG("INFO: reference payload ID %d is 0x%08X\n", i, debugconf.ref_payload[i].id);
            }

            /* global count */
            nb_pkt_received_ref[i] = 0;
        }
    }

    /* Get log file configuration */
    str = json_object_get_string(conf_obj, "log_file");
    if (str != NULL) {
        strncpy(debugconf.log_file_name, str, sizeof debugconf.log_file_name);
        debugconf.log_file_name[sizeof debugconf.log_file_name - 1] = '\0'; /* ensure string termination */
        MSG("INFO: setting debug log file name to %s\n", debugconf.log_file_name);
    }

    /* Commit configuration */
    if (lgw_debug_setconf(&debugconf) != LGW_HAL_SUCCESS) {
        MSG("ERROR: Failed to configure debug\n");
        json_value_free(root_val);
        return -1;
    }

    /* free JSON parsing data structure */
    json_value_free(root_val);
    return 0;
}

static uint16_t crc16(const uint8_t * data, unsigned size) {
    const uint16_t crc_poly = 0x1021;
    const uint16_t init_val = 0x0000;
    uint16_t x = init_val;
    unsigned i, j;

    if (data == NULL)  {
        return 0;
    }

    for (i=0; i<size; ++i) {
        x ^= (uint16_t)data[i] << 8;
        for (j=0; j<8; ++j) {
            x = (x & 0x8000) ? (x<<1) ^ crc_poly : (x<<1);
        }
    }

    return x;
}

static double difftimespec(struct timespec end, struct timespec beginning) {
    double x;

    x = 1E-9 * (double)(end.tv_nsec - beginning.tv_nsec);
    x += (double)(end.tv_sec - beginning.tv_sec);

    return x;
}

static int send_tx_ack(uint8_t token_h, uint8_t token_l, enum jit_error_e error, int32_t error_value, const tx_ack_extra_t *extra) {
    uint8_t buff_ack[ACK_BUFF_SIZE]; /* buffer to give feedback to server */
    int buff_index;
    int j;

    /* reset buffer */
    memset(&buff_ack, 0, sizeof buff_ack);

    /* Prepare downlink feedback to be sent to server */
    buff_ack[0] = PROTOCOL_VERSION;
    buff_ack[1] = token_h;
    buff_ack[2] = token_l;
    buff_ack[3] = PKT_TX_ACK;
    *(uint32_t *)(buff_ack + 4) = net_mac_h;
    *(uint32_t *)(buff_ack + 8) = net_mac_l;
    buff_index = 12; /* 12-byte header */

    /* WM1303: if extra (post-TX CAD/LBT) is provided, emit extended JSON
       regardless of error code — Python uses this to resolve its pending
       future. Fall through to legacy path if extra is NULL. */
    if (extra != NULL) {
        const char *err_name = "NONE";
        char lbt_rssi_json[16] = "null";
        if (extra->lbt_checked && extra->lbt_rssi_dbm > -128) {
            snprintf(lbt_rssi_json, sizeof lbt_rssi_json, "%d", (int)extra->lbt_rssi_dbm);
        }
        switch (error) {
            case JIT_ERROR_OK:       err_name = "NONE"; break;
            case JIT_ERROR_TX_FREQ:  err_name = "TX_FREQ"; break;
            case JIT_ERROR_TX_POWER: err_name = "TX_POWER"; break;
            case JIT_ERROR_GPS_UNLOCKED: err_name = "GPS_UNLOCKED"; break;
            default:                 err_name = "UNKNOWN"; break;
        }
        j = snprintf((char *)(buff_ack + buff_index), ACK_BUFF_SIZE - buff_index,
                     "{\"txpk_ack\":{\"error\":\"%s\",\"phase\":\"post_tx\","
                     "\"tx_result\":\"%s\","
                     "\"cad\":{\"enabled\":%s,\"detected\":%s,\"retries\":%u,"
                     "\"tx_noisefloor_dbm\":%d,\"reason\":\"%s\",\"duration_ms\":%u},"
                     "\"lbt\":{\"enabled\":%s,\"pass\":%s,\"rssi_dbm\":%s,"
                     "\"threshold_dbm\":%d,\"retries\":%u}}}",
                     err_name,
                     extra->tx_result ? extra->tx_result : "unknown",
                     extra->cad_enabled ? "true" : "false",
                     extra->cad_detected ? "true" : "false",
                     (unsigned)extra->cad_retries,
                     (int)extra->tx_noisefloor,
                     extra->cad_reason ? extra->cad_reason : "",
                     (unsigned)extra->cad_duration_ms,
                     extra->lbt_enabled ? "true" : "false",
                     extra->lbt_checked ? (extra->lbt_pass ? "true" : "false") : "null",
                     lbt_rssi_json,
                     (int)extra->lbt_threshold_dbm,
                     (unsigned)extra->lbt_retries);
        if (j > 0 && j < (int)(ACK_BUFF_SIZE - buff_index)) {
            buff_index += j;
        } else {
            MSG("WARNING: [down] send_tx_ack extra JSON truncated/failed\n");
        }
    } else if (error != JIT_ERROR_OK) {
        /* start of JSON structure */
        memcpy((void *)(buff_ack + buff_index), (void *)"{\"txpk_ack\":{", 13);
        buff_index += 13;
        /* set downlink error/warning status in JSON structure */
        switch( error ) {
            case JIT_ERROR_TX_POWER:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"warn\":", 7);
                buff_index += 7;
                break;
            default:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"error\":", 8);
                buff_index += 8;
                break;
        }
        /* set error/warning type in JSON structure */
        switch (error) {
            case JIT_ERROR_FULL:
            case JIT_ERROR_COLLISION_PACKET:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"COLLISION_PACKET\"", 18);
                buff_index += 18;
                /* update stats */
                pthread_mutex_lock(&mx_meas_dw);
                meas_nb_tx_rejected_collision_packet += 1;
                pthread_mutex_unlock(&mx_meas_dw);
                break;
            case JIT_ERROR_TOO_LATE:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"TOO_LATE\"", 10);
                buff_index += 10;
                /* update stats */
                pthread_mutex_lock(&mx_meas_dw);
                meas_nb_tx_rejected_too_late += 1;
                pthread_mutex_unlock(&mx_meas_dw);
                break;
            case JIT_ERROR_TOO_EARLY:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"TOO_EARLY\"", 11);
                buff_index += 11;
                /* update stats */
                pthread_mutex_lock(&mx_meas_dw);
                meas_nb_tx_rejected_too_early += 1;
                pthread_mutex_unlock(&mx_meas_dw);
                break;
            case JIT_ERROR_COLLISION_BEACON:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"COLLISION_BEACON\"", 18);
                buff_index += 18;
                /* update stats */
                pthread_mutex_lock(&mx_meas_dw);
                meas_nb_tx_rejected_collision_beacon += 1;
                pthread_mutex_unlock(&mx_meas_dw);
                break;
            case JIT_ERROR_TX_FREQ:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"TX_FREQ\"", 9);
                buff_index += 9;
                break;
            case JIT_ERROR_TX_POWER:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"TX_POWER\"", 10);
                buff_index += 10;
                break;
            case JIT_ERROR_GPS_UNLOCKED:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"GPS_UNLOCKED\"", 14);
                buff_index += 14;
                break;
            default:
                memcpy((void *)(buff_ack + buff_index), (void *)"\"UNKNOWN\"", 9);
                buff_index += 9;
                break;
        }
        /* set error/warning details in JSON structure */
        switch (error) {
            case JIT_ERROR_TX_POWER:
                j = snprintf((char *)(buff_ack + buff_index), ACK_BUFF_SIZE-buff_index, ",\"value\":%d", error_value);
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }
                break;
            default:
                /* Do nothing */
                break;
        }
        /* end of JSON structure */
        memcpy((void *)(buff_ack + buff_index), (void *)"}}", 2);
        buff_index += 2;
    }

    buff_ack[buff_index] = 0; /* add string terminator, for safety */

    /* send datagram to server */
    return send(sock_down, (void *)buff_ack, buff_index, 0);
}

/* -------------------------------------------------------------------------- */
/* --- MAIN FUNCTION -------------------------------------------------------- */

int main(int argc, char ** argv)
{
    struct sigaction sigact; /* SIGQUIT&SIGINT&SIGTERM signal handling */
    int i; /* loop variable and temporary variable for return value */
    int x;
    int l, m;

    /* configuration file related */
    const char defaut_conf_fname[] = JSON_CONF_DEFAULT;
    const char * conf_fname = defaut_conf_fname; /* pointer to a string we won't touch */

    /* threads */
    pthread_t thrid_up;
    pthread_t thrid_down;
    pthread_t thrid_gps;
    pthread_t thrid_valid;
    pthread_t thrid_jit;
    pthread_t thrid_ss;
    pthread_t thrid_capture;

    /* network socket creation */
    struct addrinfo hints;
    struct addrinfo *result; /* store result of getaddrinfo */
    struct addrinfo *q; /* pointer to move into *result data */
    char host_name[64];
    char port_name[64];

    /* variables to get local copies of measurements */
    uint32_t cp_nb_rx_rcv;
    uint32_t cp_nb_rx_ok;
    uint32_t cp_nb_rx_bad;
    uint32_t cp_nb_rx_nocrc;
    uint32_t cp_up_pkt_fwd;
    uint32_t cp_up_network_byte;
    uint32_t cp_up_payload_byte;
    uint32_t cp_up_dgram_sent;
    uint32_t cp_up_ack_rcv;
    uint32_t cp_dw_pull_sent;
    uint32_t cp_dw_ack_rcv;
    uint32_t cp_dw_dgram_rcv;
    uint32_t cp_dw_network_byte;
    uint32_t cp_dw_payload_byte;
    uint32_t cp_nb_tx_ok;
    uint32_t cp_nb_tx_fail;
    uint32_t cp_nb_tx_requested = 0;
    uint32_t cp_nb_tx_rejected_collision_packet = 0;
    uint32_t cp_nb_tx_rejected_collision_beacon = 0;
    uint32_t cp_nb_tx_rejected_too_late = 0;
    uint32_t cp_nb_tx_rejected_too_early = 0;
    uint32_t cp_nb_beacon_queued = 0;
    uint32_t cp_nb_beacon_sent = 0;
    uint32_t cp_nb_beacon_rejected = 0;

    /* GPS coordinates variables */
    bool coord_ok = false;
    struct coord_s cp_gps_coord = {0.0, 0.0, 0};

    /* SX1302 data variables */
    uint32_t trig_tstamp;
    uint32_t inst_tstamp;
    uint64_t eui;
    float temperature;

    /* statistics variable */
    time_t t;
    char stat_timestamp[24];
    float rx_ok_ratio;
    float rx_bad_ratio;
    float rx_nocrc_ratio;
    float up_ack_ratio;
    float dw_ack_ratio;


    /* Force line-buffered stdout for piped output */
    setvbuf(stdout, NULL, _IOLBF, 0);
    /* Seed random number generator for CAD retry jitter */
    srand((unsigned int)time(NULL));
    /* Parse command line options */
    while( (i = getopt( argc, argv, "hc:" )) != -1 )
    {
        switch( i )
        {
        case 'h':
            usage( );
            return EXIT_SUCCESS;
            break;

        case 'c':
            conf_fname = optarg;
            break;

        default:
            printf( "ERROR: argument parsing options, use -h option for help\n" );
            usage( );
            return EXIT_FAILURE;
        }
    }

    /* display version informations */
    MSG("*** Packet Forwarder ***\nVersion: " VERSION_STRING "\n");
    MSG("*** SX1302 HAL library version info ***\n%s\n***\n", lgw_version_info());

    /* display host endianness */
    #if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        MSG("INFO: Little endian host\n");
    #elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
        MSG("INFO: Big endian host\n");
    #else
        MSG("INFO: Host endianness unknown\n");
    #endif

    /* load configuration files */
    if (access(conf_fname, R_OK) == 0) { /* if there is a global conf, parse it  */
        MSG("INFO: found configuration file %s, parsing it\n", conf_fname);
        x = parse_SX130x_configuration(conf_fname);
        if (x != 0) {
            exit(EXIT_FAILURE);
        }
        x = parse_gateway_configuration(conf_fname);
        if (x != 0) {
            exit(EXIT_FAILURE);
        }
        x = parse_debug_configuration(conf_fname);
        if (x != 0) {
            MSG("INFO: no debug configuration\n");
        }
    } else {
        MSG("ERROR: [main] failed to find any configuration file named %s\n", conf_fname);
        exit(EXIT_FAILURE);
    }

    /* Start GPS a.s.a.p., to allow it to lock */
    if (gps_tty_path[0] != '\0') { /* do not try to open GPS device if no path set */
        i = lgw_gps_enable(gps_tty_path, "ubx7", 0, &gps_tty_fd); /* HAL only supports u-blox 7 for now */
        if (i != LGW_GPS_SUCCESS) {
            printf("WARNING: [main] impossible to open %s for GPS sync (check permissions)\n", gps_tty_path);
            gps_enabled = false;
            gps_ref_valid = false;
        } else {
            printf("INFO: [main] TTY port %s open for GPS synchronization\n", gps_tty_path);
            gps_enabled = true;
            gps_ref_valid = false;
        }
    }

    /* get timezone info */
    tzset();

    /* sanity check on configuration variables */
    // TODO

    /* process some of the configuration variables */
    net_mac_h = htonl((uint32_t)(0xFFFFFFFF & (lgwm>>32)));
    net_mac_l = htonl((uint32_t)(0xFFFFFFFF &  lgwm  ));

    /* prepare hints to open network sockets */
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET; /* WA: Forcing IPv4 as AF_UNSPEC makes connection on localhost to fail */
    hints.ai_socktype = SOCK_DGRAM;

    /* look for server address w/ upstream port */
    i = getaddrinfo(serv_addr, serv_port_up, &hints, &result);
    if (i != 0) {
        MSG("ERROR: [up] getaddrinfo on address %s (PORT %s) returned %s\n", serv_addr, serv_port_up, gai_strerror(i));
        exit(EXIT_FAILURE);
    }

    /* try to open socket for upstream traffic */
    for (q=result; q!=NULL; q=q->ai_next) {
        sock_up = socket(q->ai_family, q->ai_socktype,q->ai_protocol);
        if (sock_up == -1) continue; /* try next field */
        else break; /* success, get out of loop */
    }
    if (q == NULL) {
        MSG("ERROR: [up] failed to open socket to any of server %s addresses (port %s)\n", serv_addr, serv_port_up);
        i = 1;
        for (q=result; q!=NULL; q=q->ai_next) {
            getnameinfo(q->ai_addr, q->ai_addrlen, host_name, sizeof host_name, port_name, sizeof port_name, NI_NUMERICHOST);
            MSG("INFO: [up] result %i host:%s service:%s\n", i, host_name, port_name);
            ++i;
        }
        exit(EXIT_FAILURE);
    }

    /* connect so we can send/receive packet with the server only */
    i = connect(sock_up, q->ai_addr, q->ai_addrlen);
    if (i != 0) {
        MSG("ERROR: [up] connect returned %s\n", strerror(errno));
        exit(EXIT_FAILURE);
    }
    freeaddrinfo(result);

    /* look for server address w/ downstream port */
    i = getaddrinfo(serv_addr, serv_port_down, &hints, &result);
    if (i != 0) {
        MSG("ERROR: [down] getaddrinfo on address %s (port %s) returned %s\n", serv_addr, serv_port_down, gai_strerror(i));
        exit(EXIT_FAILURE);
    }

    /* try to open socket for downstream traffic */
    for (q=result; q!=NULL; q=q->ai_next) {
        sock_down = socket(q->ai_family, q->ai_socktype,q->ai_protocol);
        if (sock_down == -1) continue; /* try next field */
        else break; /* success, get out of loop */
    }
    if (q == NULL) {
        MSG("ERROR: [down] failed to open socket to any of server %s addresses (port %s)\n", serv_addr, serv_port_down);
        i = 1;
        for (q=result; q!=NULL; q=q->ai_next) {
            getnameinfo(q->ai_addr, q->ai_addrlen, host_name, sizeof host_name, port_name, sizeof port_name, NI_NUMERICHOST);
            MSG("INFO: [down] result %i host:%s service:%s\n", i, host_name, port_name);
            ++i;
        }
        exit(EXIT_FAILURE);
    }

    /* connect so we can send/receive packet with the server only */
    i = connect(sock_down, q->ai_addr, q->ai_addrlen);
    if (i != 0) {
        MSG("ERROR: [down] connect returned %s\n", strerror(errno));
        exit(EXIT_FAILURE);
    }
    freeaddrinfo(result);

    if (com_type == LGW_COM_SPI) {
        /* Board reset */
        if (system("./reset_lgw.sh start") != 0) {
            printf("ERROR: failed to reset SX1302, check your reset_lgw.sh script\n");
            exit(EXIT_FAILURE);
        }
    }

    for (l = 0; l < LGW_IF_CHAIN_NB; l++) {
        for (m = 0; m < 8; m++) {
            nb_pkt_log[l][m] = 0;
        }
    }

    /* starting the concentrator */
    i = lgw_start();
    if (i == LGW_HAL_SUCCESS) {
        MSG("INFO: [main] concentrator started, packet can now be received\n");
    } else {
        MSG("ERROR: [main] failed to start the concentrator\n");
        exit(EXIT_FAILURE);
    }

    /* get the concentrator EUI */
    i = lgw_get_eui(&eui);
    if (i != LGW_HAL_SUCCESS) {
        printf("ERROR: failed to get concentrator EUI\n");
    } else {
        printf("INFO: concentrator EUI: 0x%016" PRIx64 "\n", eui);
    }

    /* spawn threads to manage upstream and downstream */
    i = pthread_create(&thrid_up, NULL, (void * (*)(void *))thread_up, NULL);
    if (i != 0) {
        MSG("ERROR: [main] impossible to create upstream thread\n");
        exit(EXIT_FAILURE);
    }
    i = pthread_create(&thrid_down, NULL, (void * (*)(void *))thread_down, NULL);
    if (i != 0) {
        MSG("ERROR: [main] impossible to create downstream thread\n");
        exit(EXIT_FAILURE);
    }
    i = pthread_create(&thrid_jit, NULL, (void * (*)(void *))thread_jit, NULL);
    if (i != 0) {
        MSG("ERROR: [main] impossible to create JIT thread\n");
        exit(EXIT_FAILURE);
    }

    /* spawn thread for background spectral scan */
    if (spectral_scan_params.enable == true) {
        i = pthread_create(&thrid_ss, NULL, (void * (*)(void *))thread_spectral_scan, NULL);
        if (i != 0) {
            MSG("ERROR: [main] impossible to create Spectral Scan thread\n");
            exit(EXIT_FAILURE);
        }
    }

    /* spawn thread for CAPTURE_RAM streaming */
    if (capture_conf.enable == true) {
        i = pthread_create(&thrid_capture, NULL, (void * (*)(void *))thread_capture_ram, NULL);
        if (i != 0) {
            MSG("ERROR: [main] impossible to create CAPTURE_RAM thread\n");
            exit(EXIT_FAILURE);
        }
    }

    /* spawn thread to manage GPS */
    if (gps_enabled == true) {
        i = pthread_create(&thrid_gps, NULL, (void * (*)(void *))thread_gps, NULL);
        if (i != 0) {
            MSG("ERROR: [main] impossible to create GPS thread\n");
            exit(EXIT_FAILURE);
        }
        i = pthread_create(&thrid_valid, NULL, (void * (*)(void *))thread_valid, NULL);
        if (i != 0) {
            MSG("ERROR: [main] impossible to create validation thread\n");
            exit(EXIT_FAILURE);
        }
    }

    /* configure signal handling */
    sigemptyset(&sigact.sa_mask);
    sigact.sa_flags = 0;
    sigact.sa_handler = sig_handler;
    sigaction(SIGQUIT, &sigact, NULL); /* Ctrl-\ */
    sigaction(SIGINT, &sigact, NULL); /* Ctrl-C */
    sigaction(SIGTERM, &sigact, NULL); /* default "kill" command */

    /* main loop task : statistics collection */
    while (!exit_sig && !quit_sig) {
        /* wait for next reporting interval */
        wait_ms(1000 * stat_interval);

        /* get timestamp for statistics */
        t = time(NULL);
        strftime(stat_timestamp, sizeof stat_timestamp, "%F %T %Z", gmtime(&t));

        /* access upstream statistics, copy and reset them */
        pthread_mutex_lock(&mx_meas_up);
        cp_nb_rx_rcv       = meas_nb_rx_rcv;
        cp_nb_rx_ok        = meas_nb_rx_ok;
        cp_nb_rx_bad       = meas_nb_rx_bad;
        cp_nb_rx_nocrc     = meas_nb_rx_nocrc;
        cp_up_pkt_fwd      = meas_up_pkt_fwd;
        cp_up_network_byte = meas_up_network_byte;
        cp_up_payload_byte = meas_up_payload_byte;
        cp_up_dgram_sent   = meas_up_dgram_sent;
        cp_up_ack_rcv      = meas_up_ack_rcv;
        meas_nb_rx_rcv = 0;
        meas_nb_rx_ok = 0;
        meas_nb_rx_bad = 0;
        meas_nb_rx_nocrc = 0;
        meas_up_pkt_fwd = 0;
        meas_up_network_byte = 0;
        meas_up_payload_byte = 0;
        meas_up_dgram_sent = 0;
        meas_up_ack_rcv = 0;
        pthread_mutex_unlock(&mx_meas_up);
        if (cp_nb_rx_rcv > 0) {
            rx_ok_ratio = (float)cp_nb_rx_ok / (float)cp_nb_rx_rcv;
            rx_bad_ratio = (float)cp_nb_rx_bad / (float)cp_nb_rx_rcv;
            rx_nocrc_ratio = (float)cp_nb_rx_nocrc / (float)cp_nb_rx_rcv;
        } else {
            rx_ok_ratio = 0.0;
            rx_bad_ratio = 0.0;
            rx_nocrc_ratio = 0.0;
        }
        if (cp_up_dgram_sent > 0) {
            up_ack_ratio = (float)cp_up_ack_rcv / (float)cp_up_dgram_sent;
        } else {
            up_ack_ratio = 0.0;
        }

        /* access downstream statistics, copy and reset them */
        pthread_mutex_lock(&mx_meas_dw);
        cp_dw_pull_sent    =  meas_dw_pull_sent;
        cp_dw_ack_rcv      =  meas_dw_ack_rcv;
        cp_dw_dgram_rcv    =  meas_dw_dgram_rcv;
        cp_dw_network_byte =  meas_dw_network_byte;
        cp_dw_payload_byte =  meas_dw_payload_byte;
        cp_nb_tx_ok        =  meas_nb_tx_ok;
        cp_nb_tx_fail      =  meas_nb_tx_fail;
        cp_nb_tx_requested                 +=  meas_nb_tx_requested;
        cp_nb_tx_rejected_collision_packet +=  meas_nb_tx_rejected_collision_packet;
        cp_nb_tx_rejected_collision_beacon +=  meas_nb_tx_rejected_collision_beacon;
        cp_nb_tx_rejected_too_late         +=  meas_nb_tx_rejected_too_late;
        cp_nb_tx_rejected_too_early        +=  meas_nb_tx_rejected_too_early;
        cp_nb_beacon_queued   +=  meas_nb_beacon_queued;
        cp_nb_beacon_sent     +=  meas_nb_beacon_sent;
        cp_nb_beacon_rejected +=  meas_nb_beacon_rejected;
        meas_dw_pull_sent = 0;
        meas_dw_ack_rcv = 0;
        meas_dw_dgram_rcv = 0;
        meas_dw_network_byte = 0;
        meas_dw_payload_byte = 0;
        meas_nb_tx_ok = 0;
        meas_nb_tx_fail = 0;
        meas_nb_tx_requested = 0;
        meas_nb_tx_rejected_collision_packet = 0;
        meas_nb_tx_rejected_collision_beacon = 0;
        meas_nb_tx_rejected_too_late = 0;
        meas_nb_tx_rejected_too_early = 0;
        meas_nb_beacon_queued = 0;
        meas_nb_beacon_sent = 0;
        meas_nb_beacon_rejected = 0;
        pthread_mutex_unlock(&mx_meas_dw);
        if (cp_dw_pull_sent > 0) {
            dw_ack_ratio = (float)cp_dw_ack_rcv / (float)cp_dw_pull_sent;
        } else {
            dw_ack_ratio = 0.0;
        }

        /* access GPS statistics, copy them */
        if (gps_enabled == true) {
            pthread_mutex_lock(&mx_meas_gps);
            coord_ok = gps_coord_valid;
            cp_gps_coord = meas_gps_coord;
            pthread_mutex_unlock(&mx_meas_gps);
        }

        /* overwrite with reference coordinates if function is enabled */
        if (gps_fake_enable == true) {
            cp_gps_coord = reference_coord;
        }

        /* display a report */
        printf("\n##### %s #####\n", stat_timestamp);
        printf("### [UPSTREAM] ###\n");
        printf("# RF packets received by concentrator: %u\n", cp_nb_rx_rcv);
        printf("# CRC_OK: %.2f%%, CRC_FAIL: %.2f%%, NO_CRC: %.2f%%\n", 100.0 * rx_ok_ratio, 100.0 * rx_bad_ratio, 100.0 * rx_nocrc_ratio);
        printf("# RF packets forwarded: %u (%u bytes)\n", cp_up_pkt_fwd, cp_up_payload_byte);
        printf("# PUSH_DATA datagrams sent: %u (%u bytes)\n", cp_up_dgram_sent, cp_up_network_byte);
        printf("# PUSH_DATA acknowledged: %.2f%%\n", 100.0 * up_ack_ratio);
        printf("### [DOWNSTREAM] ###\n");
        printf("# PULL_DATA sent: %u (%.2f%% acknowledged)\n", cp_dw_pull_sent, 100.0 * dw_ack_ratio);
        printf("# PULL_RESP(onse) datagrams received: %u (%u bytes)\n", cp_dw_dgram_rcv, cp_dw_network_byte);
        printf("# RF packets sent to concentrator: %u (%u bytes)\n", (cp_nb_tx_ok+cp_nb_tx_fail), cp_dw_payload_byte);
        printf("# TX errors: %u\n", cp_nb_tx_fail);
        if (cp_nb_tx_requested != 0 ) {
            printf("# TX rejected (collision packet): %.2f%% (req:%u, rej:%u)\n", 100.0 * cp_nb_tx_rejected_collision_packet / cp_nb_tx_requested, cp_nb_tx_requested, cp_nb_tx_rejected_collision_packet);
            printf("# TX rejected (collision beacon): %.2f%% (req:%u, rej:%u)\n", 100.0 * cp_nb_tx_rejected_collision_beacon / cp_nb_tx_requested, cp_nb_tx_requested, cp_nb_tx_rejected_collision_beacon);
            printf("# TX rejected (too late): %.2f%% (req:%u, rej:%u)\n", 100.0 * cp_nb_tx_rejected_too_late / cp_nb_tx_requested, cp_nb_tx_requested, cp_nb_tx_rejected_too_late);
            printf("# TX rejected (too early): %.2f%% (req:%u, rej:%u)\n", 100.0 * cp_nb_tx_rejected_too_early / cp_nb_tx_requested, cp_nb_tx_requested, cp_nb_tx_rejected_too_early);
        }
        printf("### SX1302 Status ###\n");
        pthread_mutex_lock(&mx_concent);
        i  = lgw_get_instcnt(&inst_tstamp);
        i |= lgw_get_trigcnt(&trig_tstamp);
        pthread_mutex_unlock(&mx_concent);
        if (i != LGW_HAL_SUCCESS) {
            printf("# SX1302 counter unknown\n");
        } else {
            printf("# SX1302 counter (INST): %u\n", inst_tstamp);
            printf("# SX1302 counter (PPS):  %u\n", trig_tstamp);
        }
        printf("# BEACON queued: %u\n", cp_nb_beacon_queued);
        printf("# BEACON sent so far: %u\n", cp_nb_beacon_sent);
        printf("# BEACON rejected: %u\n", cp_nb_beacon_rejected);
        printf("### [JIT] ###\n");
        /* get timestamp captured on PPM pulse  */
        jit_print_queue (&jit_queue[0], false, DEBUG_LOG);
        printf("#--------\n");
        jit_print_queue (&jit_queue[1], false, DEBUG_LOG);
        printf("### [GPS] ###\n");
        if (gps_enabled == true) {
            /* no need for mutex, display is not critical */
            if (gps_ref_valid == true) {
                printf("# Valid time reference (age: %li sec)\n", (long)difftime(time(NULL), time_reference_gps.systime));
            } else {
                printf("# Invalid time reference (age: %li sec)\n", (long)difftime(time(NULL), time_reference_gps.systime));
            }
            if (coord_ok == true) {
                printf("# GPS coordinates: latitude %.5f, longitude %.5f, altitude %i m\n", cp_gps_coord.lat, cp_gps_coord.lon, cp_gps_coord.alt);
            } else {
                printf("# no valid GPS coordinates available yet\n");
            }
        } else if (gps_fake_enable == true) {
            printf("# GPS *FAKE* coordinates: latitude %.5f, longitude %.5f, altitude %i m\n", cp_gps_coord.lat, cp_gps_coord.lon, cp_gps_coord.alt);
        } else {
            printf("# GPS sync is disabled\n");
        }
        pthread_mutex_lock(&mx_concent);
        i = lgw_get_temperature(&temperature);
        pthread_mutex_unlock(&mx_concent);
        if (i != LGW_HAL_SUCCESS) {
            printf("### Concentrator temperature unknown ###\n");
        } else {
            printf("### Concentrator temperature: %.0f C ###\n", temperature);
            /* Write concentrator temperature to file for external readers */
            FILE *temp_fp = fopen("/tmp/concentrator_temp", "w");
            if (temp_fp != NULL) {
                fprintf(temp_fp, "%.1f\n", temperature);
                fclose(temp_fp);
            }
        }
        printf("##### END #####\n");

        /* generate a JSON report (will be sent to server by upstream thread) */
        pthread_mutex_lock(&mx_stat_rep);
        if (((gps_enabled == true) && (coord_ok == true)) || (gps_fake_enable == true)) {
            snprintf(status_report, STATUS_SIZE, "\"stat\":{\"time\":\"%s\",\"lati\":%.5f,\"long\":%.5f,\"alti\":%i,\"rxnb\":%u,\"rxok\":%u,\"rxfw\":%u,\"ackr\":%.1f,\"dwnb\":%u,\"txnb\":%u,\"temp\":%.1f}", stat_timestamp, cp_gps_coord.lat, cp_gps_coord.lon, cp_gps_coord.alt, cp_nb_rx_rcv, cp_nb_rx_ok, cp_up_pkt_fwd, 100.0 * up_ack_ratio, cp_dw_dgram_rcv, cp_nb_tx_ok, temperature);
        } else {
            snprintf(status_report, STATUS_SIZE, "\"stat\":{\"time\":\"%s\",\"rxnb\":%u,\"rxok\":%u,\"rxfw\":%u,\"ackr\":%.1f,\"dwnb\":%u,\"txnb\":%u,\"temp\":%.1f}", stat_timestamp, cp_nb_rx_rcv, cp_nb_rx_ok, cp_up_pkt_fwd, 100.0 * up_ack_ratio, cp_dw_dgram_rcv, cp_nb_tx_ok, temperature);
        }
        report_ready = true;
        pthread_mutex_unlock(&mx_stat_rep);
    }

    /* wait for all threads with a COM with the concentrator board to finish (1 fetch cycle max) */
    i = pthread_join(thrid_up, NULL);
    if (i != 0) {
        printf("ERROR: failed to join upstream thread with %d - %s\n", i, strerror(errno));
    }
    i = pthread_join(thrid_down, NULL);
    if (i != 0) {
        printf("ERROR: failed to join downstream thread with %d - %s\n", i, strerror(errno));
    }
    i = pthread_join(thrid_jit, NULL);
    if (i != 0) {
        printf("ERROR: failed to join JIT thread with %d - %s\n", i, strerror(errno));
    }
    if (spectral_scan_params.enable == true) {
        i = pthread_join(thrid_ss, NULL);
        if (i != 0) {
            printf("ERROR: failed to join Spectral Scan thread with %d - %s\n", i, strerror(errno));
        }
    }
    if (capture_conf.enable == true) {
        i = pthread_join(thrid_capture, NULL);
        if (i != 0) {
            printf("ERROR: failed to join CAPTURE_RAM thread with %d - %s\n", i, strerror(errno));
        }
    }
    if (gps_enabled == true) {
        pthread_cancel(thrid_gps); /* don't wait for GPS thread, no access to concentrator board */
        pthread_cancel(thrid_valid); /* don't wait for validation thread, no access to concentrator board */

        i = lgw_gps_disable(gps_tty_fd);
        if (i == LGW_HAL_SUCCESS) {
            MSG("INFO: GPS closed successfully\n");
        } else {
            MSG("WARNING: failed to close GPS successfully\n");
        }
    }

    /* if an exit signal was received, try to quit properly */
    if (exit_sig) {
        /* shut down network sockets */
        shutdown(sock_up, SHUT_RDWR);
        shutdown(sock_down, SHUT_RDWR);
        /* stop the hardware */
        i = lgw_stop();
        if (i == LGW_HAL_SUCCESS) {
            MSG("INFO: concentrator stopped successfully\n");
        } else {
            MSG("WARNING: failed to stop concentrator successfully\n");
        }
    }

    if (com_type == LGW_COM_SPI) {
        /* Board reset */
        if (system("./reset_lgw.sh stop") != 0) {
            printf("ERROR: failed to reset SX1302, check your reset_lgw.sh script\n");
            exit(EXIT_FAILURE);
        }
    }

    MSG("INFO: Exiting packet forwarder program\n");
    exit(EXIT_SUCCESS);
}

/* -------------------------------------------------------------------------- */
/* --- WM1303: SX1302 monitoring code REMOVED -------------------------------- */
/*                                                                            */
/* The periodic SX1302 register monitoring (sx1302_monitor_dump) and all      */
/* recovery escalation code (L0 burst / L1 correlator reinit / L2 full        */
/* restart / L2b semi-dead detection) were removed after empirical            */
/* investigation (2026-04-22) proved the root cause to be the custom post-TX  */
/* AGC reload block in loragw_hal.c.  Removing that block eliminated the      */
/* "snap" events entirely, and 24h+ of stable operation on a test unit       */
/* monitoring confirmed no residual issues.                                   */
/*                                                                            */
/* If problems reappear in the future, the monitoring code can be restored    */
/* from git history (commits prior to removal). The Python-side Detection 4   */
/* in wm1303_backend.py remains as a last-resort safety net.                  */
/* -------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------- */
/* --- THREAD 1: RECEIVING PACKETS AND FORWARDING THEM ---------------------- */

void thread_up(void) {
    int i, j, k; /* loop variables */
    unsigned pkt_in_dgram; /* nb on Lora packet in the current datagram */
    char stat_timestamp[24];
    time_t t;

    /* allocate memory for packet fetching and processing */
    struct lgw_pkt_rx_s rxpkt[NB_PKT_MAX]; /* array containing inbound packets + metadata */
    struct lgw_pkt_rx_s *p; /* pointer on a RX packet */
    int nb_pkt;

    /* local copy of GPS time reference */
    bool ref_ok = false; /* determine if GPS time reference must be used or not */
    struct tref local_ref; /* time reference used for UTC <-> timestamp conversion */

    /* data buffers */
    uint8_t buff_up[TX_BUFF_SIZE]; /* buffer to compose the upstream packet */
    int buff_index;
    uint8_t buff_ack[32]; /* buffer to receive acknowledges */

    /* protocol variables */
    uint8_t token_h; /* random token for acknowledgement matching */
    uint8_t token_l; /* random token for acknowledgement matching */

    /* ping measurement variables */
    struct timespec send_time;
    struct timespec recv_time;

    /* GPS synchronization variables */
    struct timespec pkt_utc_time;
    struct tm * x; /* broken-up UTC time */
    struct timespec pkt_gps_time;
    uint64_t pkt_gps_time_ms;

    /* report management variable */
    bool send_report = false;

    /* mote info variables */
    uint32_t mote_addr = 0;
    uint16_t mote_fcnt = 0;

    /* set upstream socket RX timeout */
    i = setsockopt(sock_up, SOL_SOCKET, SO_RCVTIMEO, (void *)&push_timeout_half, sizeof push_timeout_half);
    if (i != 0) {
        MSG("ERROR: [up] setsockopt returned %s\n", strerror(errno));
        exit(EXIT_FAILURE);
    }

    /* pre-fill the data buffer with fixed fields */
    buff_up[0] = PROTOCOL_VERSION;
    buff_up[3] = PKT_PUSH_DATA;
    *(uint32_t *)(buff_up + 4) = net_mac_h;
    *(uint32_t *)(buff_up + 8) = net_mac_l;

    while (!exit_sig && !quit_sig) {

        /* fetch packets */
        pthread_mutex_lock(&mx_concent);
        nb_pkt = lgw_receive(NB_PKT_MAX, rxpkt);
        pthread_mutex_unlock(&mx_concent);
        if (nb_pkt == LGW_HAL_ERROR) {
            MSG("ERROR: [up] failed packet fetch, exiting\n");
            exit(EXIT_FAILURE);
        }

        /* L1→L2 escalation: HAL signals that correlator recovery is exhausted */
        if (lgw_l2_restart_requested) {
            MSG("CRITICAL: [L2 escalation] HAL requested process restart — exiting for full hardware reset\n");
            quit_sig = true;
            break;
        }

        /* WM1303: sweep stale TX-ACK tokens every ~1 second */
        {
            static struct timespec _last_sweep = {0, 0};
            struct timespec _now_sweep;
            clock_gettime(CLOCK_MONOTONIC, &_now_sweep);
            if (difftimespec(_now_sweep, _last_sweep) >= 1.0) {
                tx_ack_token_sweep_stale();
                _last_sweep = _now_sweep;
            }
        }


        /* check if there are status report to send */
        send_report = report_ready; /* copy the variable so it doesn't change mid-function */
        /* no mutex, we're only reading */

        /* wait a short time if no packets, nor status report */
        if ((nb_pkt == 0) && (send_report == false)) {
            wait_ms(FETCH_SLEEP_MS);
            continue;
        }

        /* get a copy of GPS time reference (avoid 1 mutex per packet) */
        if ((nb_pkt > 0) && (gps_enabled == true)) {
            pthread_mutex_lock(&mx_timeref);
            ref_ok = gps_ref_valid;
            local_ref = time_reference_gps;
            pthread_mutex_unlock(&mx_timeref);
        } else {
            ref_ok = false;
        }

        /* get timestamp for statistics */
        t = time(NULL);
        strftime(stat_timestamp, sizeof stat_timestamp, "%F %T %Z", gmtime(&t));
        MSG_DEBUG(DEBUG_PKT_FWD, "\nCurrent time: %s \n", stat_timestamp);

        /* start composing datagram with the header */
        token_h = (uint8_t)rand(); /* random token */
        token_l = (uint8_t)rand(); /* random token */
        buff_up[1] = token_h;
        buff_up[2] = token_l;
        buff_index = 12; /* 12-byte header */

        /* start of JSON structure */
        memcpy((void *)(buff_up + buff_index), (void *)"{\"rxpk\":[", 9);
        buff_index += 9;

        /* serialize Lora packets metadata and payload */
        pkt_in_dgram = 0;
        for (i = 0; i < nb_pkt; ++i) {
            p = &rxpkt[i];

            /* Get mote information from current packet (addr, fcnt) */
            /* FHDR - DevAddr */
            if (p->size >= 8) {
                mote_addr  = p->payload[1];
                mote_addr |= p->payload[2] << 8;
                mote_addr |= p->payload[3] << 16;
                mote_addr |= (uint32_t)p->payload[4] << 24;
                /* FHDR - FCnt */
                mote_fcnt  = p->payload[6];
                mote_fcnt |= p->payload[7] << 8;
            } else {
                mote_addr = 0;
                mote_fcnt = 0;
            }

            /* Track recent RX activity for airtime-aware spectral scan deferral */
            {
                uint32_t toa_ms = spectral_compute_rx_toa_ms(p);
                if (toa_ms > 0) {
                    spectral_last_rx_ms = spectral_now_ms();
                    spectral_last_rx_toa_ms = toa_ms;
                }
            }

            /* basic packet filtering */
            pthread_mutex_lock(&mx_meas_up);
            meas_nb_rx_rcv += 1;
            switch(p->status) {
                case STAT_CRC_OK:
                    meas_nb_rx_ok += 1;
                    if (!fwd_valid_pkt) {
                        pthread_mutex_unlock(&mx_meas_up);
                        continue; /* skip that packet */
                    }
                    break;
                case STAT_CRC_BAD:
                    meas_nb_rx_bad += 1;
                    if (!fwd_error_pkt) {
                        pthread_mutex_unlock(&mx_meas_up);
                        continue; /* skip that packet */
                    }
                    break;
                case STAT_NO_CRC:
                    meas_nb_rx_nocrc += 1;
                    if (!fwd_nocrc_pkt) {
                        pthread_mutex_unlock(&mx_meas_up);
                        continue; /* skip that packet */
                    }
                    break;
                default:
                    MSG("WARNING: [up] received packet with unknown status %u (size %u, modulation %u, BW %u, DR %u, RSSI %.1f)\n", p->status, p->size, p->modulation, p->bandwidth, p->datarate, p->rssic);
                    pthread_mutex_unlock(&mx_meas_up);
                    continue; /* skip that packet */
                    // exit(EXIT_FAILURE);
            }
            meas_up_pkt_fwd += 1;
            meas_up_payload_byte += p->size;
            pthread_mutex_unlock(&mx_meas_up);
            printf( "\nINFO: Received pkt from mote: %08X (fcnt=%u)\n", mote_addr, mote_fcnt );

            /* Start of packet, add inter-packet separator if necessary */
            if (pkt_in_dgram == 0) {
                buff_up[buff_index] = '{';
                ++buff_index;
            } else {
                buff_up[buff_index] = ',';
                buff_up[buff_index+1] = '{';
                buff_index += 2;
            }

            /* JSON rxpk frame format version, 8 useful chars */
            j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, "\"jver\":%d", PROTOCOL_JSON_RXPK_FRAME_FORMAT );
            if (j > 0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                exit(EXIT_FAILURE);
            }

            /* RAW timestamp, 8-17 useful chars */
            j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"tmst\":%u", p->count_us);
            if (j > 0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                exit(EXIT_FAILURE);
            }

            /* Packet RX time (GPS based), 37 useful chars */
            if (ref_ok == true) {
                /* convert packet timestamp to UTC absolute time */
                j = lgw_cnt2utc(local_ref, p->count_us, &pkt_utc_time);
                if (j == LGW_GPS_SUCCESS) {
                    /* split the UNIX timestamp to its calendar components */
                    x = gmtime(&(pkt_utc_time.tv_sec));
                    j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"time\":\"%04i-%02i-%02iT%02i:%02i:%02i.%06liZ\"", (x->tm_year)+1900, (x->tm_mon)+1, x->tm_mday, x->tm_hour, x->tm_min, x->tm_sec, (pkt_utc_time.tv_nsec)/1000); /* ISO 8601 format */
                    if (j > 0) {
                        buff_index += j;
                    } else {
                        MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                        exit(EXIT_FAILURE);
                    }
                }
                /* convert packet timestamp to GPS absolute time */
                j = lgw_cnt2gps(local_ref, p->count_us, &pkt_gps_time);
                if (j == LGW_GPS_SUCCESS) {
                    pkt_gps_time_ms = pkt_gps_time.tv_sec * 1E3 + pkt_gps_time.tv_nsec / 1E6;
                    j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"tmms\":%" PRIu64 "", pkt_gps_time_ms); /* GPS time in milliseconds since 06.Jan.1980 */
                    if (j > 0) {
                        buff_index += j;
                    } else {
                        MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                        exit(EXIT_FAILURE);
                    }
                }
            }

            /* Fine timestamp */
            if (p->ftime_received == true) {
                j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"ftime\":%u", p->ftime);
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }
            }

            /* Packet concentrator channel, RF chain & RX frequency, 34-36 useful chars */
            j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"chan\":%1u,\"rfch\":%1u,\"freq\":%.6lf,\"mid\":%2u", p->if_chain, p->rf_chain, ((double)p->freq_hz / 1e6), p->modem_id);
            if (j > 0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                exit(EXIT_FAILURE);
            }

            /* Packet status, 9-10 useful chars */
            switch (p->status) {
                case STAT_CRC_OK:
                    memcpy((void *)(buff_up + buff_index), (void *)",\"stat\":1", 9);
                    buff_index += 9;
                    break;
                case STAT_CRC_BAD:
                    memcpy((void *)(buff_up + buff_index), (void *)",\"stat\":-1", 10);
                    buff_index += 10;
                    break;
                case STAT_NO_CRC:
                    memcpy((void *)(buff_up + buff_index), (void *)",\"stat\":0", 9);
                    buff_index += 9;
                    break;
                default:
                    MSG("ERROR: [up] received packet with unknown status 0x%02X\n", p->status);
                    memcpy((void *)(buff_up + buff_index), (void *)",\"stat\":?", 9);
                    buff_index += 9;
                    exit(EXIT_FAILURE);
            }

            /* Packet modulation, 13-14 useful chars */
            if (p->modulation == MOD_LORA) {
                memcpy((void *)(buff_up + buff_index), (void *)",\"modu\":\"LORA\"", 14);
                buff_index += 14;

                /* Lora datarate & bandwidth, 16-19 useful chars */
                switch (p->datarate) {
                    case DR_LORA_SF5:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF5", 12);
                        buff_index += 12;
                        break;
                    case DR_LORA_SF6:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF6", 12);
                        buff_index += 12;
                        break;
                    case DR_LORA_SF7:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF7", 12);
                        buff_index += 12;
                        break;
                    case DR_LORA_SF8:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF8", 12);
                        buff_index += 12;
                        break;
                    case DR_LORA_SF9:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF9", 12);
                        buff_index += 12;
                        break;
                    case DR_LORA_SF10:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF10", 13);
                        buff_index += 13;
                        break;
                    case DR_LORA_SF11:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF11", 13);
                        buff_index += 13;
                        break;
                    case DR_LORA_SF12:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF12", 13);
                        buff_index += 13;
                        break;
                    default:
                        MSG("ERROR: [up] lora packet with unknown datarate 0x%02X\n", p->datarate);
                        memcpy((void *)(buff_up + buff_index), (void *)",\"datr\":\"SF?", 12);
                        buff_index += 12;
                        exit(EXIT_FAILURE);
                }
                switch (p->bandwidth) {
                    case BW_125KHZ:
                        memcpy((void *)(buff_up + buff_index), (void *)"BW125\"", 6);
                        buff_index += 6;
                        break;
                    case BW_250KHZ:
                        memcpy((void *)(buff_up + buff_index), (void *)"BW250\"", 6);
                        buff_index += 6;
                        break;
                    case BW_500KHZ:
                        memcpy((void *)(buff_up + buff_index), (void *)"BW500\"", 6);
                        buff_index += 6;
                        break;
                    case BW_62K5HZ:
                        memcpy((void *)(buff_up + buff_index), (void *)"BW62\"", 5);
                        buff_index += 5;
                        break;
                    default:
                        MSG("WARNING: [up] lora packet with unknown bandwidth 0x%02X\n", p->bandwidth);
                        memcpy((void *)(buff_up + buff_index), (void *)"BW?\"", 4);
                        buff_index += 4;
                        break; /* was exit(EXIT_FAILURE) */
                }

                /* Packet ECC coding rate, 11-13 useful chars */
                switch (p->coderate) {
                    case CR_LORA_4_5:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"4/5\"", 13);
                        buff_index += 13;
                        break;
                    case CR_LORA_4_6:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"4/6\"", 13);
                        buff_index += 13;
                        break;
                    case CR_LORA_4_7:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"4/7\"", 13);
                        buff_index += 13;
                        break;
                    case CR_LORA_4_8:
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"4/8\"", 13);
                        buff_index += 13;
                        break;
                    case 0: /* treat the CR0 case (mostly false sync) */
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"OFF\"", 13);
                        buff_index += 13;
                        break;
                    default:
                        MSG("ERROR: [up] lora packet with unknown coderate 0x%02X\n", p->coderate);
                        memcpy((void *)(buff_up + buff_index), (void *)",\"codr\":\"?\"", 11);
                        buff_index += 11;
                        exit(EXIT_FAILURE);
                }

                /* Signal RSSI, payload size */
                j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"rssis\":%.0f", roundf(p->rssis));
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }

                /* Lora SNR */
                j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"lsnr\":%.1f", p->snr);
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }

                /* Lora frequency offset */
                j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"foff\":%d", p->freq_offset);
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }
            } else if (p->modulation == MOD_FSK) {
                memcpy((void *)(buff_up + buff_index), (void *)",\"modu\":\"FSK\"", 13);
                buff_index += 13;

                /* FSK datarate, 11-14 useful chars */
                j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"datr\":%u", p->datarate);
                if (j > 0) {
                    buff_index += j;
                } else {
                    MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                    exit(EXIT_FAILURE);
                }
            } else {
                MSG("ERROR: [up] received packet with unknown modulation 0x%02X\n", p->modulation);
                exit(EXIT_FAILURE);
            }

            /* Channel RSSI, payload size, 18-23 useful chars */
            j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, ",\"rssi\":%.0f,\"size\":%u", roundf(p->rssic), p->size);
            if (j > 0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 4));
                exit(EXIT_FAILURE);
            }

            /* Packet base64-encoded payload, 14-350 useful chars */
            memcpy((void *)(buff_up + buff_index), (void *)",\"data\":\"", 9);
            buff_index += 9;
            j = bin_to_b64(p->payload, p->size, (char *)(buff_up + buff_index), 341); /* 255 bytes = 340 chars in b64 + null char */
            if (j>=0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] bin_to_b64 failed line %d\n", (__LINE__ - 5));
                exit(EXIT_FAILURE);
            }
            buff_up[buff_index] = '"';
            ++buff_index;

            /* End of packet serialization */
            buff_up[buff_index] = '}';
            ++buff_index;
            ++pkt_in_dgram;

            if (p->modulation == MOD_LORA) {
                /* Log nb of packets per channel, per SF */
                nb_pkt_log[p->if_chain][p->datarate - 5] += 1;
                nb_pkt_received_lora += 1;

                /* Log nb of packets for ref_payload (DEBUG) */
                for (k = 0; k < debugconf.nb_ref_payload; k++) {
                    if ((p->payload[0] == (uint8_t)(debugconf.ref_payload[k].id >> 24)) &&
                        (p->payload[1] == (uint8_t)(debugconf.ref_payload[k].id >> 16)) &&
                        (p->payload[2] == (uint8_t)(debugconf.ref_payload[k].id >> 8))  &&
                        (p->payload[3] == (uint8_t)(debugconf.ref_payload[k].id >> 0))) {
                            nb_pkt_received_ref[k] += 1;
                        }
                }
            } else if (p->modulation == MOD_FSK) {
                nb_pkt_log[p->if_chain][0] += 1;
                nb_pkt_received_fsk += 1;
            }
        }


        /* DEBUG: print the number of packets received per channel and per SF */
        {
            int l, m;
            MSG_PRINTF(DEBUG_PKT_FWD, "\n");
            for (l = 0; l < (LGW_IF_CHAIN_NB - 1); l++) {
                MSG_PRINTF(DEBUG_PKT_FWD, "CH%d: ", l);
                for (m = 0; m < 8; m++) {
                    MSG_PRINTF(DEBUG_PKT_FWD, "\t%d", nb_pkt_log[l][m]);
                }
                MSG_PRINTF(DEBUG_PKT_FWD, "\n");
            }
            MSG_PRINTF(DEBUG_PKT_FWD, "FSK: \t%d", nb_pkt_log[9][0]);
            MSG_PRINTF(DEBUG_PKT_FWD, "\n");
            MSG_PRINTF(DEBUG_PKT_FWD, "Total number of LoRa packet received: %u\n", nb_pkt_received_lora);
            MSG_PRINTF(DEBUG_PKT_FWD, "Total number of FSK packet received: %u\n", nb_pkt_received_fsk);
            for (l = 0; l < debugconf.nb_ref_payload; l++) {
                MSG_PRINTF(DEBUG_PKT_FWD, "Total number of LoRa packet received from 0x%08X: %u\n", debugconf.ref_payload[l].id, nb_pkt_received_ref[l]);
            }
        }

        /* restart fetch sequence without sending empty JSON if all packets have been filtered out */
        if (pkt_in_dgram == 0) {
            if (send_report == true) {
                /* need to clean up the beginning of the payload */
                buff_index -= 8; /* removes "rxpk":[ */
            } else {
                /* all packet have been filtered out and no report, restart loop */
                continue;
            }
        } else {
            /* end of packet array */
            buff_up[buff_index] = ']';
            ++buff_index;
            /* add separator if needed */
            if (send_report == true) {
                buff_up[buff_index] = ',';
                ++buff_index;
            }
        }

        /* add status report if a new one is available */
        if (send_report == true) {
            pthread_mutex_lock(&mx_stat_rep);
            report_ready = false;
            j = snprintf((char *)(buff_up + buff_index), TX_BUFF_SIZE-buff_index, "%s", status_report);
            pthread_mutex_unlock(&mx_stat_rep);
            if (j > 0) {
                buff_index += j;
            } else {
                MSG("ERROR: [up] snprintf failed line %d\n", (__LINE__ - 5));
                exit(EXIT_FAILURE);
            }
        }

        /* end of JSON datagram payload */
        buff_up[buff_index] = '}';
        ++buff_index;
        buff_up[buff_index] = 0; /* add string terminator, for safety */

        printf("\nJSON up: %s\n", (char *)(buff_up + 12)); /* DEBUG: display JSON payload */

        /* send datagram to server */
        send(sock_up, (void *)buff_up, buff_index, 0);
        clock_gettime(CLOCK_MONOTONIC, &send_time);
        pthread_mutex_lock(&mx_meas_up);
        meas_up_dgram_sent += 1;
        meas_up_network_byte += buff_index;

        /* wait for acknowledge (in 2 times, to catch extra packets) */
        for (i=0; i<2; ++i) {
            j = recv(sock_up, (void *)buff_ack, sizeof buff_ack, 0);
            clock_gettime(CLOCK_MONOTONIC, &recv_time);
            if (j == -1) {
                if (errno == EAGAIN) { /* timeout */
                    continue;
                } else { /* server connection error */
                    break;
                }
            } else if ((j < 4) || (buff_ack[0] != PROTOCOL_VERSION) || (buff_ack[3] != PKT_PUSH_ACK)) {
                //MSG("WARNING: [up] ignored invalid non-ACL packet\n");
                continue;
            } else if ((buff_ack[1] != token_h) || (buff_ack[2] != token_l)) {
                //MSG("WARNING: [up] ignored out-of sync ACK packet\n");
                continue;
            } else {
                MSG("INFO: [up] PUSH_ACK received in %i ms\n", (int)(1000 * difftimespec(recv_time, send_time)));
                meas_up_ack_rcv += 1;
                break;
            }
        }
        pthread_mutex_unlock(&mx_meas_up);
    }
    MSG("\nINFO: End of upstream thread\n");
}

/* -------------------------------------------------------------------------- */
/* --- THREAD 2: POLLING SERVER AND ENQUEUING PACKETS IN JIT QUEUE ---------- */

static int get_tx_gain_lut_index(uint8_t rf_chain, int8_t rf_power, uint8_t * lut_index) {
    uint8_t pow_index;
    int current_best_index = -1;
    uint8_t current_best_match = 0xFF;
    int diff;

    /* Check input parameters */
    if (lut_index == NULL) {
        MSG("ERROR: %s - wrong parameter\n", __FUNCTION__);
        return -1;
    }

    /* Search requested power in TX gain LUT */
    for (pow_index = 0; pow_index < txlut[rf_chain].size; pow_index++) {
        diff = rf_power - txlut[rf_chain].lut[pow_index].rf_power;
        if (diff < 0) {
            /* The selected power must be lower or equal to requested one */
            continue;
        } else {
            /* Record the index corresponding to the closest rf_power available in LUT */
            if ((current_best_index == -1) || (diff < current_best_match)) {
                current_best_match = diff;
                current_best_index = pow_index;
            }
        }
    }

    /* Return corresponding index */
    if (current_best_index > -1) {
        *lut_index = (uint8_t)current_best_index;
    } else {
        *lut_index = 0;
        MSG("ERROR: %s - failed to find tx gain lut index\n", __FUNCTION__);
        return -1;
    }

    return 0;
}

void thread_down(void) {
    int i; /* loop variables */

    /* configuration and metadata for an outbound packet */
    struct lgw_pkt_tx_s txpkt;
    bool sent_immediate = false; /* option to sent the packet immediately */

    /* local timekeeping variables */
    struct timespec send_time; /* time of the pull request */
    struct timespec recv_time; /* time of return from recv socket call */

    /* data buffers */
    uint8_t buff_down[1000]; /* buffer to receive downstream packets */
    uint8_t buff_req[12]; /* buffer to compose pull requests */
    int msg_len;

    /* protocol variables */
    uint8_t token_h; /* random token for acknowledgement matching */
    uint8_t token_l; /* random token for acknowledgement matching */
    bool req_ack = false; /* keep track of whether PULL_DATA was acknowledged or not */

    /* JSON parsing variables */
    JSON_Value *root_val = NULL;
    JSON_Object *txpk_obj = NULL;
    JSON_Value *val = NULL; /* needed to detect the absence of some fields */
    const char *str; /* pointer to sub-strings in the JSON data */
    short x0, x1;
    uint64_t x2;
    double x3, x4;

    /* variables to send on GPS timestamp */
    struct tref local_ref; /* time reference used for GPS <-> timestamp conversion */
    struct timespec gps_tx; /* GPS time that needs to be converted to timestamp */

    /* beacon variables */
    struct lgw_pkt_tx_s beacon_pkt;
    uint8_t beacon_chan;
    uint8_t beacon_loop;
    size_t beacon_RFU1_size = 0;
    size_t beacon_RFU2_size = 0;
    uint8_t beacon_pyld_idx = 0;
    time_t diff_beacon_time;
    struct timespec next_beacon_gps_time; /* gps time of next beacon packet */
    struct timespec last_beacon_gps_time; /* gps time of last enqueued beacon packet */
    int retry;

    /* beacon data fields, byte 0 is Least Significant Byte */
    int32_t field_latitude; /* 3 bytes, derived from reference latitude */
    int32_t field_longitude; /* 3 bytes, derived from reference longitude */
    uint16_t field_crc1, field_crc2;

    /* auto-quit variable */
    uint32_t autoquit_cnt = 0; /* count the number of PULL_DATA sent since the latest PULL_ACK */

    /* Just In Time downlink */
    uint32_t current_concentrator_time;
    enum jit_error_e jit_result = JIT_ERROR_OK;
    enum jit_pkt_type_e downlink_type;
    enum jit_error_e warning_result = JIT_ERROR_OK;
    int32_t warning_value = 0;
    uint8_t tx_lut_idx = 0;

    /* set downstream socket RX timeout */
    i = setsockopt(sock_down, SOL_SOCKET, SO_RCVTIMEO, (void *)&pull_timeout, sizeof pull_timeout);
    if (i != 0) {
        MSG("ERROR: [down] setsockopt returned %s\n", strerror(errno));
        exit(EXIT_FAILURE);
    }

    /* pre-fill the pull request buffer with fixed fields */
    buff_req[0] = PROTOCOL_VERSION;
    buff_req[3] = PKT_PULL_DATA;
    *(uint32_t *)(buff_req + 4) = net_mac_h;
    *(uint32_t *)(buff_req + 8) = net_mac_l;

    /* beacon variables initialization */
    last_beacon_gps_time.tv_sec = 0;
    last_beacon_gps_time.tv_nsec = 0;

    /* beacon packet parameters */
    beacon_pkt.tx_mode = ON_GPS; /* send on PPS pulse */
    beacon_pkt.rf_chain = 0; /* antenna A */
    beacon_pkt.rf_power = beacon_power;
    beacon_pkt.modulation = MOD_LORA;
    switch (beacon_bw_hz) {
        case 125000:
            beacon_pkt.bandwidth = BW_125KHZ;
            break;
        case 500000:
            beacon_pkt.bandwidth = BW_500KHZ;
            break;
        default:
            /* should not happen */
            MSG("ERROR: unsupported bandwidth for beacon\n");
            exit(EXIT_FAILURE);
    }
    switch (beacon_datarate) {
        case 8:
            beacon_pkt.datarate = DR_LORA_SF8;
            beacon_RFU1_size = 1;
            beacon_RFU2_size = 3;
            break;
        case 9:
            beacon_pkt.datarate = DR_LORA_SF9;
            beacon_RFU1_size = 2;
            beacon_RFU2_size = 0;
            break;
        case 10:
            beacon_pkt.datarate = DR_LORA_SF10;
            beacon_RFU1_size = 3;
            beacon_RFU2_size = 1;
            break;
        case 12:
            beacon_pkt.datarate = DR_LORA_SF12;
            beacon_RFU1_size = 5;
            beacon_RFU2_size = 3;
            break;
        default:
            /* should not happen */
            MSG("ERROR: unsupported datarate for beacon\n");
            exit(EXIT_FAILURE);
    }
    beacon_pkt.size = beacon_RFU1_size + 4 + 2 + 7 + beacon_RFU2_size + 2;
    beacon_pkt.coderate = CR_LORA_4_5;
    beacon_pkt.invert_pol = false;
    beacon_pkt.preamble = 10;
    beacon_pkt.no_crc = true;
    beacon_pkt.no_header = true;

    /* network common part beacon fields (little endian) */
    for (i = 0; i < (int)beacon_RFU1_size; i++) {
        beacon_pkt.payload[beacon_pyld_idx++] = 0x0;
    }

    /* network common part beacon fields (little endian) */
    beacon_pyld_idx += 4; /* time (variable), filled later */
    beacon_pyld_idx += 2; /* crc1 (variable), filled later */

    /* calculate the latitude and longitude that must be publicly reported */
    field_latitude = (int32_t)((reference_coord.lat / 90.0) * (double)(1<<23));
    if (field_latitude > (int32_t)0x007FFFFF) {
        field_latitude = (int32_t)0x007FFFFF; /* +90 N is represented as 89.99999 N */
    } else if (field_latitude < (int32_t)0xFF800000) {
        field_latitude = (int32_t)0xFF800000;
    }
    field_longitude = (int32_t)((reference_coord.lon / 180.0) * (double)(1<<23));
    if (field_longitude > (int32_t)0x007FFFFF) {
        field_longitude = (int32_t)0x007FFFFF; /* +180 E is represented as 179.99999 E */
    } else if (field_longitude < (int32_t)0xFF800000) {
        field_longitude = (int32_t)0xFF800000;
    }

    /* gateway specific beacon fields */
    beacon_pkt.payload[beacon_pyld_idx++] = beacon_infodesc;
    /* Encode signed coordinates modulo 2^24 without right-shifting a negative signed value. */
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)field_latitude;
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)((uint32_t)field_latitude >>  8);
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)((uint32_t)field_latitude >> 16);
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)field_longitude;
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)((uint32_t)field_longitude >>  8);
    beacon_pkt.payload[beacon_pyld_idx++] = (uint8_t)((uint32_t)field_longitude >> 16);

    /* RFU */
    for (i = 0; i < (int)beacon_RFU2_size; i++) {
        beacon_pkt.payload[beacon_pyld_idx++] = 0x0;
    }

    /* CRC of the beacon gateway specific part fields */
    field_crc2 = crc16((beacon_pkt.payload + 6 + beacon_RFU1_size), 7 + beacon_RFU2_size);
    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF &  field_crc2;
    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & (field_crc2 >> 8);

    /* JIT queue initialization */
    jit_queue_init(&jit_queue[0]);
    jit_queue_init(&jit_queue[1]);

    while (!exit_sig && !quit_sig) {

        /* auto-quit if the threshold is crossed */
        if ((autoquit_threshold > 0) && (autoquit_cnt >= autoquit_threshold)) {
            exit_sig = true;
            MSG("INFO: [down] the last %u PULL_DATA were not ACKed, exiting application\n", autoquit_threshold);
            break;
        }

        /* generate random token for request */
        token_h = (uint8_t)rand(); /* random token */
        token_l = (uint8_t)rand(); /* random token */
        buff_req[1] = token_h;
        buff_req[2] = token_l;

        /* send PULL request and record time */
        send(sock_down, (void *)buff_req, sizeof buff_req, 0);
        clock_gettime(CLOCK_MONOTONIC, &send_time);
        pthread_mutex_lock(&mx_meas_dw);
        meas_dw_pull_sent += 1;
        pthread_mutex_unlock(&mx_meas_dw);
        req_ack = false;
        autoquit_cnt++;

        /* listen to packets and process them until a new PULL request must be sent */
        recv_time = send_time;
        while (((int)difftimespec(recv_time, send_time) < keepalive_time) && !exit_sig && !quit_sig) {

            /* try to receive a datagram */
            msg_len = recv(sock_down, (void *)buff_down, (sizeof buff_down)-1, 0);
            clock_gettime(CLOCK_MONOTONIC, &recv_time);

            /* Pre-allocate beacon slots in JiT queue, to check downlink collisions */
            beacon_loop = JIT_NUM_BEACON_IN_QUEUE - jit_queue[0].num_beacon;
            retry = 0;
            while (beacon_loop && (beacon_period != 0)) {
                pthread_mutex_lock(&mx_timeref);
                /* Wait for GPS to be ready before inserting beacons in JiT queue */
                if ((gps_ref_valid == true) && (xtal_correct_ok == true)) {

                    /* compute GPS time for next beacon to come      */
                    /*   LoRaWAN: T = k*beacon_period + TBeaconDelay */
                    /*            with TBeaconDelay = [1.5ms +/- 1µs]*/
                    if (last_beacon_gps_time.tv_sec == 0) {
                        /* if no beacon has been queued, get next slot from current GPS time */
                        diff_beacon_time = time_reference_gps.gps.tv_sec % ((time_t)beacon_period);
                        next_beacon_gps_time.tv_sec = time_reference_gps.gps.tv_sec +
                                                        ((time_t)beacon_period - diff_beacon_time);
                    } else {
                        /* if there is already a beacon, take it as reference */
                        next_beacon_gps_time.tv_sec = last_beacon_gps_time.tv_sec + beacon_period;
                    }
                    /* now we can add a beacon_period to the reference to get next beacon GPS time */
                    next_beacon_gps_time.tv_sec += (retry * beacon_period);
                    next_beacon_gps_time.tv_nsec = 0;

#if DEBUG_BEACON
                    {
                    time_t time_unix;

                    time_unix = time_reference_gps.gps.tv_sec + UNIX_GPS_EPOCH_OFFSET;
                    MSG_DEBUG(DEBUG_BEACON, "GPS-now : %s", ctime(&time_unix));
                    time_unix = last_beacon_gps_time.tv_sec + UNIX_GPS_EPOCH_OFFSET;
                    MSG_DEBUG(DEBUG_BEACON, "GPS-last: %s", ctime(&time_unix));
                    time_unix = next_beacon_gps_time.tv_sec + UNIX_GPS_EPOCH_OFFSET;
                    MSG_DEBUG(DEBUG_BEACON, "GPS-next: %s", ctime(&time_unix));
                    }
#endif

                    /* convert GPS time to concentrator time, and set packet counter for JiT trigger */
                    lgw_gps2cnt(time_reference_gps, next_beacon_gps_time, &(beacon_pkt.count_us));
                    pthread_mutex_unlock(&mx_timeref);

                    /* apply frequency correction to beacon TX frequency */
                    if (beacon_freq_nb > 1) {
                        beacon_chan = (next_beacon_gps_time.tv_sec / beacon_period) % beacon_freq_nb; /* floor rounding */
                    } else {
                        beacon_chan = 0;
                    }
                    /* Compute beacon frequency */
                    beacon_pkt.freq_hz = beacon_freq_hz + (beacon_chan * beacon_freq_step);

                    /* load time in beacon payload */
                    beacon_pyld_idx = beacon_RFU1_size;
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF &  next_beacon_gps_time.tv_sec;
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & (next_beacon_gps_time.tv_sec >>  8);
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & (next_beacon_gps_time.tv_sec >> 16);
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & (next_beacon_gps_time.tv_sec >> 24);

                    /* calculate CRC */
                    field_crc1 = crc16(beacon_pkt.payload, 4 + beacon_RFU1_size); /* CRC for the network common part */
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & field_crc1;
                    beacon_pkt.payload[beacon_pyld_idx++] = 0xFF & (field_crc1 >> 8);

                    /* Insert beacon packet in JiT queue */
                    pthread_mutex_lock(&mx_concent);
                    lgw_get_instcnt(&current_concentrator_time);
                    pthread_mutex_unlock(&mx_concent);
                    jit_result = jit_enqueue(&jit_queue[0], current_concentrator_time, &beacon_pkt, JIT_PKT_TYPE_BEACON);
                    if (jit_result == JIT_ERROR_OK) {
                        /* update stats */
                        pthread_mutex_lock(&mx_meas_dw);
                        meas_nb_beacon_queued += 1;
                        pthread_mutex_unlock(&mx_meas_dw);

                        /* One more beacon in the queue */
                        beacon_loop--;
                        retry = 0;
                        last_beacon_gps_time.tv_sec = next_beacon_gps_time.tv_sec; /* keep this beacon time as reference for next one to be programmed */

                        /* display beacon payload */
                        MSG("INFO: Beacon queued (count_us=%u, freq_hz=%u, size=%u):\n", beacon_pkt.count_us, beacon_pkt.freq_hz, beacon_pkt.size);
                        printf( "   => " );
                        for (i = 0; i < beacon_pkt.size; ++i) {
                            MSG("%02X ", beacon_pkt.payload[i]);
                        }
                        MSG("\n");
                    } else {
                        MSG_DEBUG(DEBUG_BEACON, "--> beacon queuing failed with %d\n", jit_result);
                        /* update stats */
                        pthread_mutex_lock(&mx_meas_dw);
                        if (jit_result != JIT_ERROR_COLLISION_BEACON) {
                            meas_nb_beacon_rejected += 1;
                        }
                        pthread_mutex_unlock(&mx_meas_dw);
                        /* In case previous enqueue failed, we retry one period later until it succeeds */
                        /* Note: In case the GPS has been unlocked for a while, there can be lots of retries */
                        /*       to be done from last beacon time to a new valid one */
                        retry++;
                        MSG_DEBUG(DEBUG_BEACON, "--> beacon queuing retry=%d\n", retry);
                    }
                } else {
                    pthread_mutex_unlock(&mx_timeref);
                    break;
                }
            }

            /* if no network message was received, got back to listening sock_down socket */
            if (msg_len == -1) {
                //MSG("WARNING: [down] recv returned %s\n", strerror(errno)); /* too verbose */
                continue;
            }

            /* if the datagram does not respect protocol, just ignore it */
            if ((msg_len < 4) || (buff_down[0] != PROTOCOL_VERSION) || ((buff_down[3] != PKT_PULL_RESP) && (buff_down[3] != PKT_PULL_ACK))) {
                MSG("WARNING: [down] ignoring invalid packet len=%d, protocol_version=%d, id=%d\n",
                        msg_len, buff_down[0], buff_down[3]);
                continue;
            }

            /* if the datagram is an ACK, check token */
            if (buff_down[3] == PKT_PULL_ACK) {
                if ((buff_down[1] == token_h) && (buff_down[2] == token_l)) {
                    if (req_ack) {
                        MSG("INFO: [down] duplicate ACK received :)\n");
                    } else { /* if that packet was not already acknowledged */
                        req_ack = true;
                        autoquit_cnt = 0;
                        pthread_mutex_lock(&mx_meas_dw);
                        meas_dw_ack_rcv += 1;
                        pthread_mutex_unlock(&mx_meas_dw);
                        MSG("INFO: [down] PULL_ACK received in %i ms\n", (int)(1000 * difftimespec(recv_time, send_time)));
                    }
                } else { /* out-of-sync token */
                    MSG("INFO: [down] received out-of-sync ACK\n");
                }
                continue;
            }

            /* the datagram is a PULL_RESP */
            buff_down[msg_len] = 0; /* add string terminator, just to be safe */
            MSG("INFO: [down] PULL_RESP received  - token[%d:%d] :)\n", buff_down[1], buff_down[2]); /* very verbose */
            printf("\nJSON down: %s\n", (char *)(buff_down + 4)); /* DEBUG: display JSON payload */

            /* initialize TX struct and try to parse JSON */
            memset(&txpkt, 0, sizeof txpkt);
            root_val = json_parse_string_with_comments((const char *)(buff_down + 4)); /* JSON offset */
            if (root_val == NULL) {
                MSG("WARNING: [down] invalid JSON, TX aborted\n");
                continue;
            }

            /* look for JSON sub-object 'txpk' */
            txpk_obj = json_object_get_object(json_value_get_object(root_val), "txpk");
            if (txpk_obj == NULL) {
                MSG("WARNING: [down] no \"txpk\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }

            /* Parse "immediate" tag, or target timestamp, or UTC time to be converted by GPS (mandatory) */
            i = json_object_get_boolean(txpk_obj,"imme"); /* can be 1 if true, 0 if false, or -1 if not a JSON boolean */
            if (i == 1) {
                /* TX procedure: send immediately */
                sent_immediate = true;
                downlink_type = JIT_PKT_TYPE_DOWNLINK_CLASS_C;
                MSG("INFO: [down] a packet will be sent in \"immediate\" mode\n");
            } else {
                sent_immediate = false;
                val = json_object_get_value(txpk_obj,"tmst");
                if (val != NULL) {
                    /* TX procedure: send on timestamp value */
                    txpkt.count_us = (uint32_t)json_value_get_number(val);

                    /* Concentrator timestamp is given, we consider it is a Class A downlink */
                    downlink_type = JIT_PKT_TYPE_DOWNLINK_CLASS_A;
                } else {
                    /* TX procedure: send on GPS time (converted to timestamp value) */
                    val = json_object_get_value(txpk_obj, "tmms");
                    if (val == NULL) {
                        MSG("WARNING: [down] no mandatory \"txpk.tmst\" or \"txpk.tmms\" objects in JSON, TX aborted\n");
                        json_value_free(root_val);
                        continue;
                    }
                    if (gps_enabled == true) {
                        pthread_mutex_lock(&mx_timeref);
                        if (gps_ref_valid == true) {
                            local_ref = time_reference_gps;
                            pthread_mutex_unlock(&mx_timeref);
                        } else {
                            pthread_mutex_unlock(&mx_timeref);
                            MSG("WARNING: [down] no valid GPS time reference yet, impossible to send packet on specific GPS time, TX aborted\n");
                            json_value_free(root_val);

                            /* send acknoledge datagram to server */
                            send_tx_ack(buff_down[1], buff_down[2], JIT_ERROR_GPS_UNLOCKED, 0, NULL);
                            continue;
                        }
                    } else {
                        MSG("WARNING: [down] GPS disabled, impossible to send packet on specific GPS time, TX aborted\n");
                        json_value_free(root_val);

                        /* send acknoledge datagram to server */
                        send_tx_ack(buff_down[1], buff_down[2], JIT_ERROR_GPS_UNLOCKED, 0, NULL);
                        continue;
                    }

                    /* Get GPS time from JSON */
                    x2 = (uint64_t)json_value_get_number(val);

                    /* Convert GPS time from milliseconds to timespec */
                    x3 = modf((double)x2/1E3, &x4);
                    gps_tx.tv_sec = (time_t)x4; /* get seconds from integer part */
                    gps_tx.tv_nsec = (long)(x3 * 1E9); /* get nanoseconds from fractional part */

                    /* transform GPS time to timestamp */
                    i = lgw_gps2cnt(local_ref, gps_tx, &(txpkt.count_us));
                    if (i != LGW_GPS_SUCCESS) {
                        MSG("WARNING: [down] could not convert GPS time to timestamp, TX aborted\n");
                        json_value_free(root_val);
                        continue;
                    } else {
                        MSG("INFO: [down] a packet will be sent on timestamp value %u (calculated from GPS time)\n", txpkt.count_us);
                    }

                    /* GPS timestamp is given, we consider it is a Class B downlink */
                    downlink_type = JIT_PKT_TYPE_DOWNLINK_CLASS_B;
                }
            }

            /* Parse "No CRC" flag (optional field) */
            val = json_object_get_value(txpk_obj,"ncrc");
            if (val != NULL) {
                txpkt.no_crc = (bool)json_value_get_boolean(val);
            }

            /* Parse "No header" flag (optional field) */
            val = json_object_get_value(txpk_obj,"nhdr");
            if (val != NULL) {
                txpkt.no_header = (bool)json_value_get_boolean(val);
            }

            /* parse target frequency (mandatory) */
            val = json_object_get_value(txpk_obj,"freq");
            if (val == NULL) {
                MSG("WARNING: [down] no mandatory \"txpk.freq\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }
            txpkt.freq_hz = (uint32_t)((double)(1.0e6) * json_value_get_number(val));

            /* parse RF chain used for TX (mandatory) */
            val = json_object_get_value(txpk_obj,"rfch");
            if (val == NULL) {
                MSG("WARNING: [down] no mandatory \"txpk.rfch\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }
            txpkt.rf_chain = (uint8_t)json_value_get_number(val);
            if (tx_enable[txpkt.rf_chain] == false) {
                MSG("WARNING: [down] TX is not enabled on RF chain %u, TX aborted\n", txpkt.rf_chain);
                json_value_free(root_val);
                continue;
            }

            /* parse TX power (optional field) */
            val = json_object_get_value(txpk_obj,"powe");
            if (val != NULL) {
                txpkt.rf_power = (int8_t)json_value_get_number(val) - antenna_gain;
            }

            /* Parse modulation (mandatory) */
            str = json_object_get_string(txpk_obj, "modu");
            if (str == NULL) {
                MSG("WARNING: [down] no mandatory \"txpk.modu\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }
            if (strcmp(str, "LORA") == 0) {
                /* Lora modulation */
                txpkt.modulation = MOD_LORA;

                /* Parse Lora spreading-factor and modulation bandwidth (mandatory) */
                str = json_object_get_string(txpk_obj, "datr");
                if (str == NULL) {
                    MSG("WARNING: [down] no mandatory \"txpk.datr\" object in JSON, TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }
                i = sscanf(str, "SF%2hdBW%3hd", &x0, &x1);
                if (i != 2) {
                    MSG("WARNING: [down] format error in \"txpk.datr\", TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }
                switch (x0) {
                    case  5: txpkt.datarate = DR_LORA_SF5;  break;
                    case  6: txpkt.datarate = DR_LORA_SF6;  break;
                    case  7: txpkt.datarate = DR_LORA_SF7;  break;
                    case  8: txpkt.datarate = DR_LORA_SF8;  break;
                    case  9: txpkt.datarate = DR_LORA_SF9;  break;
                    case 10: txpkt.datarate = DR_LORA_SF10; break;
                    case 11: txpkt.datarate = DR_LORA_SF11; break;
                    case 12: txpkt.datarate = DR_LORA_SF12; break;
                    default:
                        MSG("WARNING: [down] format error in \"txpk.datr\", invalid SF, TX aborted\n");
                        json_value_free(root_val);
                        continue;
                }
                switch (x1) {
                    case 125: txpkt.bandwidth = BW_125KHZ; break;
                    case 250: txpkt.bandwidth = BW_250KHZ; break;
                    case 500: txpkt.bandwidth = BW_500KHZ; break;
                    case  62: txpkt.bandwidth = BW_62K5HZ; break;
                    default:
                        MSG("WARNING: [down] format error in \"txpk.datr\", invalid BW, TX aborted\n");
                        json_value_free(root_val);
                        continue;
                }

                /* Parse ECC coding rate (optional field) */
                str = json_object_get_string(txpk_obj, "codr");
                if (str == NULL) {
                    MSG("WARNING: [down] no mandatory \"txpk.codr\" object in json, TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }
                if      (strcmp(str, "4/5") == 0) txpkt.coderate = CR_LORA_4_5;
                else if (strcmp(str, "4/6") == 0) txpkt.coderate = CR_LORA_4_6;
                else if (strcmp(str, "2/3") == 0) txpkt.coderate = CR_LORA_4_6;
                else if (strcmp(str, "4/7") == 0) txpkt.coderate = CR_LORA_4_7;
                else if (strcmp(str, "4/8") == 0) txpkt.coderate = CR_LORA_4_8;
                else if (strcmp(str, "1/2") == 0) txpkt.coderate = CR_LORA_4_8;
                else {
                    MSG("WARNING: [down] format error in \"txpk.codr\", TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }

                /* Parse signal polarity switch (optional field) */
                val = json_object_get_value(txpk_obj,"ipol");
                if (val != NULL) {
                    txpkt.invert_pol = (bool)json_value_get_boolean(val);
                }

                /* parse Lora preamble length (optional field, optimum min value enforced) */
                val = json_object_get_value(txpk_obj,"prea");
                if (val != NULL) {
                    i = (int)json_value_get_number(val);
                    if (i >= MIN_LORA_PREAMB) {
                        txpkt.preamble = (uint16_t)i;
                    } else {
                        txpkt.preamble = (uint16_t)MIN_LORA_PREAMB;
                    }
                } else {
                    txpkt.preamble = (uint16_t)STD_LORA_PREAMB;
                }

            } else if (strcmp(str, "FSK") == 0) {
                /* FSK modulation */
                txpkt.modulation = MOD_FSK;

                /* parse FSK bitrate (mandatory) */
                val = json_object_get_value(txpk_obj,"datr");
                if (val == NULL) {
                    MSG("WARNING: [down] no mandatory \"txpk.datr\" object in JSON, TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }
                txpkt.datarate = (uint32_t)(json_value_get_number(val));

                /* parse frequency deviation (mandatory) */
                val = json_object_get_value(txpk_obj,"fdev");
                if (val == NULL) {
                    MSG("WARNING: [down] no mandatory \"txpk.fdev\" object in JSON, TX aborted\n");
                    json_value_free(root_val);
                    continue;
                }
                txpkt.f_dev = (uint8_t)(json_value_get_number(val) / 1000.0); /* JSON value in Hz, txpkt.f_dev in kHz */

                /* parse FSK preamble length (optional field, optimum min value enforced) */
                val = json_object_get_value(txpk_obj,"prea");
                if (val != NULL) {
                    i = (int)json_value_get_number(val);
                    if (i >= MIN_FSK_PREAMB) {
                        txpkt.preamble = (uint16_t)i;
                    } else {
                        txpkt.preamble = (uint16_t)MIN_FSK_PREAMB;
                    }
                } else {
                    txpkt.preamble = (uint16_t)STD_FSK_PREAMB;
                }

            } else {
                MSG("WARNING: [down] invalid modulation in \"txpk.modu\", TX aborted\n");
                json_value_free(root_val);
                continue;
            }

            /* Parse payload length (mandatory) */
            val = json_object_get_value(txpk_obj,"size");
            if (val == NULL) {
                MSG("WARNING: [down] no mandatory \"txpk.size\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }
            txpkt.size = (uint16_t)json_value_get_number(val);

            /* Parse payload data (mandatory) */
            str = json_object_get_string(txpk_obj, "data");
            if (str == NULL) {
                MSG("WARNING: [down] no mandatory \"txpk.data\" object in JSON, TX aborted\n");
                json_value_free(root_val);
                continue;
            }
            i = b64_to_bin(str, strlen(str), txpkt.payload, sizeof txpkt.payload);
            if (i != txpkt.size) {
                MSG("WARNING: [down] mismatch between .size and .data size once converter to binary\n");
            }

            /* free the JSON parse tree from memory */
            json_value_free(root_val);

            /* select TX mode */
            if (sent_immediate) {
                txpkt.tx_mode = IMMEDIATE;
            } else {
                txpkt.tx_mode = TIMESTAMPED;
            }

            /* record measurement data */
            pthread_mutex_lock(&mx_meas_dw);
            meas_dw_dgram_rcv += 1; /* count only datagrams with no JSON errors */
            meas_dw_network_byte += msg_len; /* meas_dw_network_byte */
            meas_dw_payload_byte += txpkt.size;
            pthread_mutex_unlock(&mx_meas_dw);

            /* reset error/warning results */
            jit_result = warning_result = JIT_ERROR_OK;
            warning_value = 0;

            /* check TX frequency before trying to queue packet */
            if ((txpkt.freq_hz < tx_freq_min[txpkt.rf_chain]) || (txpkt.freq_hz > tx_freq_max[txpkt.rf_chain])) {
                jit_result = JIT_ERROR_TX_FREQ;
                MSG("ERROR: Packet REJECTED, unsupported frequency - %u (min:%u,max:%u)\n", txpkt.freq_hz, tx_freq_min[txpkt.rf_chain], tx_freq_max[txpkt.rf_chain]);
            }

            /* check TX power before trying to queue packet, send a warning if not supported */
            if (jit_result == JIT_ERROR_OK) {
                i = get_tx_gain_lut_index(txpkt.rf_chain, txpkt.rf_power, &tx_lut_idx);
                if ((i < 0) || (txlut[txpkt.rf_chain].lut[tx_lut_idx].rf_power != txpkt.rf_power)) {
                    /* this RF power is not supported, throw a warning, and use the closest lower power supported */
                    warning_result = JIT_ERROR_TX_POWER;
                    warning_value = (int32_t)txlut[txpkt.rf_chain].lut[tx_lut_idx].rf_power;
                    printf("WARNING: Requested TX power is not supported (%ddBm), actual power used: %ddBm\n", txpkt.rf_power, warning_value);
                    txpkt.rf_power = txlut[txpkt.rf_chain].lut[tx_lut_idx].rf_power;
                }
            }

            /* WM1303: For IMMEDIATE mode, bypass JIT and send directly.
               The JIT converts IMMEDIATE→TIMESTAMPED+80ms which causes
               drops when the concentrator counter resets during L1/L1.5
               recovery.  Direct send eliminates this failure mode. */
            if (jit_result == JIT_ERROR_OK && sent_immediate) {
                uint8_t tx_status;
                int wait_loops = 0;
                const int max_wait = 50; /* 50 * 10ms = 500ms max wait */

                /* Wait for TX_FREE with timeout */
                pthread_mutex_lock(&mx_concent);
                do {
                    lgw_status(txpkt.rf_chain, TX_STATUS, &tx_status);
                    if (tx_status == TX_FREE) break;
                    pthread_mutex_unlock(&mx_concent);
                    wait_ms(10);
                    pthread_mutex_lock(&mx_concent);
                    wait_loops++;
                } while (wait_loops < max_wait);

                if (tx_status != TX_FREE) {
                    pthread_mutex_unlock(&mx_concent);
                    jit_result = JIT_ERROR_TOO_LATE;
                    MSG("WARNING: [imme] TX not free after %dms, dropping packet\n", wait_loops * 10);
                } else {
                    /* === WM1303: Pre-TX CAD/LBT check (same as JIT path) ===
                       CAD is MANDATORY for every TX.  LBT only when enabled
                       for the target channel in the per-channel config. */
                    tx_ack_extra_t imme_extra = {0};
                    imme_extra.cad_reason = "not_run";
                    imme_extra.tx_result  = "unknown";
                    bool imme_blocked = false;

                    /* Abort any running spectral scan so SX1261 is available */
                    if (spectral_scan_params.enable == true) {
                        int ss_err = lgw_spectral_scan_abort();
                        if (ss_err != LGW_HAL_SUCCESS) {
                            MSG("WARNING: [imme] lgw_spectral_scan_abort failed\n");
                        }
                    }

                    /* --- MANDATORY CAD check with retry ---
                       CAD is pure LoRa preamble detection. HAL LBT (RSSI-based)
                       is handled inside lgw_send() below. */
                    {
                        int cad_bw = custom_lbt_hal_bw_to_cad(txpkt.bandwidth);
                        if (cad_bw >= 0) {
                            imme_extra.cad_enabled = true;
                            const int CAD_MAX_RETRIES = 15;
                            struct timeval cad_t0, cad_t1;
                            gettimeofday(&cad_t0, NULL);
                            int cad_retry = 0;
                            bool cad_clear = false;
                            int16_t first_noisefloor = -128; /* saved from first CAD scan */

                            while (cad_retry <= CAD_MAX_RETRIES) {
                                /* Inhibit SX1261 LoRa RX before each CAD scan */
                                sx1261_set_tx_inhibit_rx(true);

                                sx1261_cad_result_t cad_result;
                                int cad_err = sx1261_cad_scan(txpkt.freq_hz, txpkt.datarate,
                                                              (uint8_t)cad_bw, &cad_result,
                                                              cad_retry > 0); /* skip_noisefloor on retries */
                                if (cad_err != 0) {
                                    MSG("WARNING: [imme] CAD scan failed (err=%d) — proceeding with TX\n", cad_err);
                                    cad_clear = true;
                                    imme_extra.cad_reason   = "scan_error";
                                    imme_extra.cad_retries  = (uint8_t)cad_retry;
                                    imme_extra.cad_detected = false;
                                    break;
                                }

                                /* Save noisefloor from first scan */
                                if (cad_retry == 0) first_noisefloor = cad_result.rssi_dbm;

                                /* CAD activity check */
                                if (!cad_result.detected) {
                                    /* Channel clear */
                                    if (cad_retry == 0) {
                                        MSG("INFO: [imme] CAD clear (freq=%u, tx_nf=%d)\n",
                                            txpkt.freq_hz, cad_result.rssi_dbm);
                                    } else {
                                        MSG("INFO: [imme] CAD clear after %d retries (freq=%u, tx_nf=%d)\n",
                                            cad_retry, txpkt.freq_hz, first_noisefloor);
                                    }
                                    cad_clear = true;
                                    imme_extra.cad_detected  = false;
                                    imme_extra.cad_retries   = (uint8_t)cad_retry;
                                    imme_extra.tx_noisefloor = (cad_result.rssi_dbm > -128) ? cad_result.rssi_dbm : first_noisefloor;
                                    imme_extra.cad_reason    = (cad_retry == 0) ? "clear" : "cleared_after_retries";
                                    /* Keep inhibit ON — proceeding to lgw_send */
                                    break;
                                }

                                /* CAD detected activity — restore RX during wait */
                                if (cad_retry < CAD_MAX_RETRIES) {
                                    int cad_wait = 1 + (rand() % 10); /* random 1-10 ms */
                                    MSG("INFO: [imme] CAD DETECTED (freq=%u, SF%u, tx_nf=%d) — retry %d/%d, wait %d ms\n",
                                        txpkt.freq_hz, txpkt.datarate, first_noisefloor,
                                        cad_retry + 1, CAD_MAX_RETRIES, cad_wait);
                                    sx1261_set_tx_inhibit_rx(false);
                                    sx1261_lora_rx_restart_light();
                                    pthread_mutex_unlock(&mx_concent);
                                    wait_ms(cad_wait);
                                    pthread_mutex_lock(&mx_concent);
                                    cad_retry++;
                                } else {
                                    /* Max retries — force TX, inhibit stays ON */
                                    MSG("WARNING: [imme] CAD still active after %d retries (freq=%u, tx_nf=%d) — FORCING TX\n",
                                        CAD_MAX_RETRIES, txpkt.freq_hz, first_noisefloor);
                                    cad_clear = true;
                                    imme_extra.cad_detected  = true;
                                    imme_extra.cad_retries   = (uint8_t)cad_retry;
                                    imme_extra.tx_noisefloor = (cad_result.rssi_dbm > -128) ? cad_result.rssi_dbm : first_noisefloor;
                                    imme_extra.cad_reason    = "forced_after_retries";
                                    break;
                                }
                            }
                            gettimeofday(&cad_t1, NULL);
                            imme_extra.cad_duration_ms = (uint32_t)((cad_t1.tv_sec - cad_t0.tv_sec) * 1000 + (cad_t1.tv_usec - cad_t0.tv_usec) / 1000);

                            if (!cad_clear) {
                                imme_blocked = true;
                            }
                        } else {
                            MSG_DEBUG(DEBUG_PKT_FWD, "[imme] Unsupported BW 0x%02x for CAD, skipping\n",
                                      txpkt.bandwidth);
                            imme_extra.cad_reason = "unsupported_bw";
                        }
                    }

                    /* --- WM1303: HAL LBT takes over here ---
                       The real LBT decision happens inside lgw_send() (via
                       lgw_lbt_start/tx_status on the SX1261 using the per-channel
                       rssi_target_dbm configured at startup). Pre-TX extra fields
                       are populated from the HAL LBT config snapshot; lbt_pass is
                       set from the lgw_send() return value below. */
                    hal_lbt_lookup(txpkt.freq_hz, txpkt.bandwidth,
                                   &imme_extra.lbt_enabled,
                                   &imme_extra.lbt_threshold_dbm);
                    imme_extra.lbt_rssi_dbm = -128; /* unknown until lgw_send confirms a result */


                    /* --- Self-signal verification --- */
                    if (imme_blocked) {
                        uint8_t verify_status = TX_STATUS_UNKNOWN;
                        lgw_status(txpkt.rf_chain, TX_STATUS, &verify_status);
                        if (verify_status == TX_EMITTING) {
                            MSG("INFO: [imme] LBT block overridden — TX_EMITTING (self-signal)\n");
                            imme_blocked = false;
                        }
                    }

                    /* --- Handle blocked TX --- */
                    if (imme_blocked) {
                        sx1261_set_tx_inhibit_rx(false);
                        sx1261_lora_rx_restart_light();
                        pthread_mutex_unlock(&mx_concent);

                        pthread_mutex_lock(&mx_meas_dw);
                        meas_nb_tx_fail += 1;
                        pthread_mutex_unlock(&mx_meas_dw);

                        MSG("INFO: [imme] TX BLOCKED by CAD/LBT (freq=%u) — skipping\n", txpkt.freq_hz);
                        imme_extra.tx_result = "blocked";
                        send_tx_ack(buff_down[1], buff_down[2], JIT_ERROR_OK, warning_value, &imme_extra);

                        jit_result = JIT_ERROR_TOO_LATE; /* prevent JIT fallback */
                        continue;
                    }

                    /* === Direct send (CAD/LBT clear or forced) === */
                    /* SX1261 TX inhibit is already ON from CAD loop */
                    int send_err = lgw_send(&txpkt);
                    hal_lbt_capture_result(&imme_extra, send_err);
                    gettimeofday(&lgw_forensic_last_tx, NULL); /* forensic: track TX event */
                    pthread_mutex_unlock(&mx_concent);

                    if (send_err == LGW_HAL_SUCCESS) {
                        MSG("INFO: [imme] direct TX sent (freq=%u, SF%u, %u bytes)\n",
                            txpkt.freq_hz, txpkt.datarate, txpkt.size);
                        /* Estimate airtime for guard and RX restart scheduling */
                        uint32_t imme_airtime_ms = 500; /* default fallback */
                        if (txpkt.bandwidth == BW_125KHZ) {
                            switch (txpkt.datarate) {
                                case DR_LORA_SF7:  imme_airtime_ms = 50 + txpkt.size * 2;   break;
                                case DR_LORA_SF8:  imme_airtime_ms = 100 + txpkt.size * 3;  break;
                                case DR_LORA_SF9:  imme_airtime_ms = 200 + txpkt.size * 5;  break;
                                case DR_LORA_SF10: imme_airtime_ms = 400 + txpkt.size * 9;  break;
                                case DR_LORA_SF11: imme_airtime_ms = 800 + txpkt.size * 17; break;
                                case DR_LORA_SF12: imme_airtime_ms = 1600 + txpkt.size * 33; break;
                                default: imme_airtime_ms = 500; break;
                            }
                        } else if (txpkt.bandwidth == BW_250KHZ) {
                            switch (txpkt.datarate) {
                                case DR_LORA_SF7:  imme_airtime_ms = 30 + txpkt.size * 1;   break;
                                case DR_LORA_SF8:  imme_airtime_ms = 50 + txpkt.size * 2;   break;
                                default: imme_airtime_ms = 250; break;
                            }
                        } else if (txpkt.bandwidth == BW_500KHZ) {
                            imme_airtime_ms = 25 + txpkt.size * 1;
                        } else {
                            /* BW_62K5HZ (Channel E) */
                            switch (txpkt.datarate) {
                                case DR_LORA_SF7:  imme_airtime_ms = 100 + txpkt.size * 4;  break;
                                case DR_LORA_SF8:  imme_airtime_ms = 200 + txpkt.size * 7;  break;
                                case DR_LORA_SF9:  imme_airtime_ms = 400 + txpkt.size * 13; break;
                                default: imme_airtime_ms = 1000; break;
                            }
                        }


                        /* Event-driven SX1261 RX restart (Option D):
                           Poll lgw_status(TX_STATUS) until the SX1302 reports
                           TX_FREE, which is the actual hardware-truth moment
                           the RF emission ends. This replaces the blind
                           usleep(airtime+20ms) with event-driven wait that
                           saves the safety margin plus any airtime overestimate.
                           Hard-cap timeout (airtime+100ms) prevents infinite hang. */
                        {
                            uint8_t _tx_stat = 0;
                            uint32_t _poll_count = 0;
                            uint32_t _max_wait_ms = imme_airtime_ms + 100;
                            uint32_t _poll_interval_us = 2000; /* 2ms */
                            uint32_t _max_polls = (_max_wait_ms * 1000) / _poll_interval_us;
                            int _poll_err = 0;
                            bool _tx_done = false;
                            /* Short initial delay so we don't poll the very first ms */
                            usleep(5000);
                            while (_poll_count < _max_polls) {
                                pthread_mutex_lock(&mx_concent);
                                _poll_err = lgw_status(txpkt.rf_chain, TX_STATUS, &_tx_stat);
                                pthread_mutex_unlock(&mx_concent);
                                if (_poll_err == LGW_HAL_SUCCESS && _tx_stat == TX_FREE) {
                                    _tx_done = true;
                                    /* 5ms FEM settling before re-enabling SX1261 RX */
                                    usleep(5000);
                                    break;
                                }
                                usleep(_poll_interval_us);
                                _poll_count++;
                            }
                            if (!_tx_done) {
                                MSG("WARNING: [imme] TX_DONE polling timed out after %u ms (airtime estimate was %u)\n",
                                    _max_wait_ms, imme_airtime_ms);
                            }
                            pthread_mutex_lock(&mx_concent);
                            sx1261_set_tx_inhibit_rx(false);
                            sx1261_lora_rx_restart_light();
                            pthread_mutex_unlock(&mx_concent);
                            custom_lbt_rx_restart_pending = false;
                            MSG("INFO: [imme] SX1261 LoRa RX restarted (TX_DONE polled, %u polls, airtime_est=%u ms)\n",
                                _poll_count, imme_airtime_ms);
                        }

                        /* Emit post-TX ACK with CAD/LBT results */
                        imme_extra.tx_result = "sent";
                        send_tx_ack(buff_down[1], buff_down[2], JIT_ERROR_OK, warning_value, &imme_extra);

                        pthread_mutex_lock(&mx_meas_dw);
                        meas_nb_tx_requested += 1;
                        meas_nb_tx_ok += 1;
                        pthread_mutex_unlock(&mx_meas_dw);

                        /* Skip the normal JIT enqueue + ACK path */
                        continue;
                    } else {
                        /* TX failed — HAL LBT may have blocked, or other HAL error.
                           Release SX1261 inhibit, restart RX, emit a post-TX TX_ACK
                           with the failure so the Python future resolves immediately. */
                        sx1261_set_tx_inhibit_rx(false);
                        sx1261_lora_rx_restart_light();
                        jit_result = JIT_ERROR_TOO_LATE; /* prevent JIT fallback */
                        if (send_err == LGW_LBT_NOT_ALLOWED) {
                            imme_extra.tx_result = "blocked";
                            MSG("INFO: [imme] HAL LBT BLOCKED TX (freq=%u, thr=%d dBm)\n",
                                txpkt.freq_hz, imme_extra.lbt_threshold_dbm);
                        } else {
                            imme_extra.tx_result = "send_failed";
                            MSG("ERROR: [imme] lgw_send() failed (err=%d)\n", send_err);
                        }
                        send_tx_ack(buff_down[1], buff_down[2], JIT_ERROR_OK, warning_value, &imme_extra);
                        pthread_mutex_lock(&mx_meas_dw);
                        meas_nb_tx_fail += 1;
                        pthread_mutex_unlock(&mx_meas_dw);
                        continue;
                    }
                }
            }

            /* Fallback: use JIT queue for timestamped packets or if direct send failed */
            if (jit_result == JIT_ERROR_OK) {
                pthread_mutex_lock(&mx_concent);
                lgw_get_instcnt(&current_concentrator_time);
                pthread_mutex_unlock(&mx_concent);
                jit_result = jit_enqueue(&jit_queue[txpkt.rf_chain], current_concentrator_time, &txpkt, downlink_type);
                if (jit_result != JIT_ERROR_OK) {
                    printf("ERROR: Packet REJECTED (jit error=%d)\n", jit_result);
                } else {
                    /* WM1303: remember the TX_ACK token for this packet so the
                       JIT thread can emit a post-TX ACK with CAD/LBT results. */
                    tx_ack_token_store(txpkt.rf_chain, buff_down[1], buff_down[2], &txpkt);
                    /* In case of a warning having been raised before, we notify it */
                    jit_result = warning_result;
                }
                pthread_mutex_lock(&mx_meas_dw);
                meas_nb_tx_requested += 1;
                pthread_mutex_unlock(&mx_meas_dw);
            }

            /* Send acknoledge datagram to server */
            send_tx_ack(buff_down[1], buff_down[2], jit_result, warning_value, NULL);
        }
    }
    MSG("\nINFO: End of downstream thread\n");
}

void print_tx_status(uint8_t tx_status) {
    switch (tx_status) {
        case TX_OFF:
            MSG("INFO: [jit] lgw_status returned TX_OFF\n");
            break;
        case TX_FREE:
            MSG("INFO: [jit] lgw_status returned TX_FREE\n");
            break;
        case TX_EMITTING:
            MSG("INFO: [jit] lgw_status returned TX_EMITTING\n");
            break;
        case TX_SCHEDULED:
            MSG("INFO: [jit] lgw_status returned TX_SCHEDULED\n");
            break;
        default:
            MSG("INFO: [jit] lgw_status returned UNKNOWN (%d)\n", tx_status);
            break;
    }
}


/* -------------------------------------------------------------------------- */
/* --- THREAD 3: CHECKING PACKETS TO BE SENT FROM JIT QUEUE AND SEND THEM --- */

void thread_jit(void) {
    int result = LGW_HAL_SUCCESS;
    struct lgw_pkt_tx_s pkt;
    int pkt_index = -1;
    uint32_t current_concentrator_time;
    enum jit_error_e jit_result;
    enum jit_pkt_type_e pkt_type;
    uint8_t tx_status;
    int i;

    while (!exit_sig && !quit_sig) {
        wait_ms(1); /* WM1303: reduced from 10ms to 1ms for faster TX pickup */

        /* Safety fallback: if a blocking RX restart was somehow missed
           (should not happen), catch it here and restart immediately. */
        if (custom_lbt_rx_restart_pending) {
            struct timespec now_ts;
            clock_gettime(CLOCK_MONOTONIC, &now_ts);
            if (difftimespec(now_ts, custom_lbt_rx_restart_after) >= 0) {
                pthread_mutex_lock(&mx_concent);
                sx1261_set_tx_inhibit_rx(false);
                sx1261_lora_rx_restart_light();
                pthread_mutex_unlock(&mx_concent);
                custom_lbt_rx_restart_pending = false;
                MSG("WARNING: [jit] SX1261 RX restart fallback triggered (blocking restart missed!)\n");
            } else {
                /* Still waiting — skip TX pickup this iteration */
                continue;
            }
        }

        for (i = 0; i < LGW_RF_CHAIN_NB; i++) {
            /* transfer data and metadata to the concentrator, and schedule TX */
            pthread_mutex_lock(&mx_concent);
            lgw_get_instcnt(&current_concentrator_time);
            pthread_mutex_unlock(&mx_concent);
            jit_result = jit_peek(&jit_queue[i], current_concentrator_time, &pkt_index);
            if (jit_result == JIT_ERROR_OK) {
                if (pkt_index > -1) {
                    jit_result = jit_dequeue(&jit_queue[i], pkt_index, &pkt, &pkt_type);
                    if (jit_result == JIT_ERROR_OK) {
                        /* update beacon stats */
                        if (pkt_type == JIT_PKT_TYPE_BEACON) {
                            /* Compensate breacon frequency with xtal error */
                            pthread_mutex_lock(&mx_xcorr);
                            pkt.freq_hz = (uint32_t)(xtal_correct * (double)pkt.freq_hz);
                            MSG_DEBUG(DEBUG_BEACON, "beacon_pkt.freq_hz=%u (xtal_correct=%.15lf)\n", pkt.freq_hz, xtal_correct);
                            pthread_mutex_unlock(&mx_xcorr);

                            /* Update statistics */
                            pthread_mutex_lock(&mx_meas_dw);
                            meas_nb_beacon_sent += 1;
                            pthread_mutex_unlock(&mx_meas_dw);
                            MSG("INFO: Beacon dequeued (count_us=%u)\n", pkt.count_us);
                        }

                        /* WM1303: Single mutex lock for status check + pre-TX block
                           to eliminate one lock/unlock cycle (~5-10ms saving) */
                        pthread_mutex_lock(&mx_concent);
                        result = lgw_status(pkt.rf_chain, TX_STATUS, &tx_status);
                        if (result == LGW_HAL_ERROR) {
                            pthread_mutex_unlock(&mx_concent);
                            MSG("WARNING: [jit%d] lgw_status failed\n", i);
                            continue; /* cannot proceed without valid status */
                        } else if (tx_status == TX_EMITTING) {
                            pthread_mutex_unlock(&mx_concent);
                            MSG("ERROR: concentrator is currently emitting on rf_chain %d\n", i);
                            print_tx_status(tx_status);
                            continue;
                        }
                        /* tx_status is TX_FREE, TX_OFF or TX_SCHEDULED — mutex stays locked */
                        if (tx_status == TX_SCHEDULED) {
                            MSG("WARNING: [jit] TX stuck in TX_SCHEDULED on rf_chain %d — aborting before CAD\n", i);
                            print_tx_status(tx_status);
                            /* WM1303: Clear stuck TX_SCHEDULED state.
                               After a CAD scan, the SX1302 TX FSM can get stuck in
                               TX_SCHEDULED (0x91) and never transition to TX_EMITTING.
                               lgw_abort_tx() clears all TX triggers and waits for TX_FREE,
                               ensuring a clean state before the next CAD + lgw_send(). */
                            int abort_err = lgw_abort_tx(pkt.rf_chain);
                            if (abort_err != LGW_HAL_SUCCESS) {
                                MSG("WARNING: [jit] lgw_abort_tx failed on rf_chain %d\n", i);
                            } else {
                                MSG("INFO: [jit] TX abort successful, TX FSM cleared on rf_chain %d\n", i);
                            }
                        }
                        if (spectral_scan_params.enable == true) {
                            result = lgw_spectral_scan_abort();
                            if (result != LGW_HAL_SUCCESS) {
                                MSG("WARNING: [jit%d] lgw_spectral_scan_abort failed\n", i);
                            }
                        }
                        /* --- WM1303: Mandatory Pre-TX CAD — always check before sending --- */
                        /* Pre-TX channel check using the SX1261:
                           1. Look up per-channel config for this frequency (RSSI/LBT only)
                           2. CAD check (MANDATORY — always runs before every TX)
                           3. RSSI check (optional — per-channel LBT config)
                           Both CAD and RSSI handle SX1261 state internally.
                           Note: HW TX_EMITTING is already checked above (consolidated mutex
                           block) so no redundant status check needed here. */
                        /* WM1303: CAD/HAL-LBT result capture for post-TX TX_ACK.
                           Populated throughout the CAD retry loop and finalized
                           after lgw_send() returns (HAL LBT pass/fail). */
                        tx_ack_extra_t extra = {0};
                        extra.cad_reason = "not_run";
                        extra.tx_result = "unknown";
                        bool clbt_blocked = false;
                        {

                            /* Note: TX inhibit is set/cleared inside the CAD retry loop
                               to maximize Channel E RX availability. Per-channel CAD
                               still consults custom_lbt_channels[] for freq_hz matching;
                               the struct retains the CAD-enabled bit after LBT cleanup. */

                            /* --- Step 3: MANDATORY CAD check with retry --- */
                            if (!clbt_blocked) {
                                int cad_bw = custom_lbt_hal_bw_to_cad(pkt.bandwidth);
                                if (cad_bw >= 0) {
                                    extra.cad_enabled = true;
                                    const int CAD_MAX_RETRIES = 15;
                                    struct timeval cad_t0, cad_t1;
                                    gettimeofday(&cad_t0, NULL);
                                    int cad_retry = 0;
                                    int cad_wait_ms __attribute__((unused)) = 0;
                                    bool cad_clear = false;
                                    int16_t first_noisefloor = -128; /* saved from first CAD scan */
                                    uint8_t lbt_retries_in_cad = 0;

                                    while (cad_retry <= CAD_MAX_RETRIES) {
                                        /* Inhibit SX1261 LoRa RX before each CAD scan */
                                        sx1261_set_tx_inhibit_rx(true);

                                        sx1261_cad_result_t cad_result;
                                        int cad_err = sx1261_cad_scan(pkt.freq_hz, pkt.datarate,
                                                                      (uint8_t)cad_bw, &cad_result,
                                                                      cad_retry > 0); /* skip_noisefloor on retries */
                                        if (cad_err != 0) {
                                            MSG("WARNING: [jit] CAD scan failed on rf_chain %d "
                                                "(err=%d) — proceeding with TX\n", i, cad_err);
                                            cad_clear = true;
                                            extra.cad_reason = "scan_error";
                                            extra.cad_retries = (uint8_t)cad_retry;
                                            extra.cad_detected = false;
                                            /* Keep inhibit ON — going to lgw_send */
                                            break;
                                        }
                                        /* Save noisefloor from first scan */
                                        if (cad_retry == 0) first_noisefloor = cad_result.rssi_dbm;

                                        if (!cad_result.detected) {
                                            if (cad_retry == 0) {
                                                MSG("INFO: [jit] CAD clear on rf_chain %d "
                                                    "(freq=%u Hz, tx_nf=%d dBm)\n",
                                                    i, pkt.freq_hz, cad_result.rssi_dbm);
                                            } else {
                                                MSG("INFO: [jit] CAD clear on rf_chain %d after %d "
                                                    "retries (freq=%u Hz, tx_nf=%d dBm)\n",
                                                    i, cad_retry, pkt.freq_hz, first_noisefloor);
                                            }
                                            cad_clear = true;
                                            extra.cad_detected = false;
                                            extra.cad_retries = (uint8_t)cad_retry;
                                            extra.tx_noisefloor = (cad_result.rssi_dbm > -128) ? cad_result.rssi_dbm : first_noisefloor;
                                            extra.cad_reason = (cad_retry == 0) ? "clear" : "cleared_after_retries";
                                            /* Keep inhibit ON — going to lgw_send */
                                            break;
                                        }

                                        /* CAD detected activity — restore RX during wait */
                                        if (cad_retry < CAD_MAX_RETRIES) {
                                            int cad_wait = 1 + (rand() % 10); /* random 1-10 ms */
                                            MSG("INFO: [jit] CAD DETECTED on rf_chain %d "
                                                "(freq=%u Hz, SF%u, tx_nf=%d dBm) — retry %d/%d, "
                                                "waiting %d ms\n",
                                                i, pkt.freq_hz, pkt.datarate,
                                                first_noisefloor,
                                                cad_retry + 1, CAD_MAX_RETRIES, cad_wait);
                                            sx1261_set_tx_inhibit_rx(false);
                                            sx1261_lora_rx_restart_light();
                                            /* Release mutex during wait */
                                            pthread_mutex_unlock(&mx_concent);
                                            wait_ms(cad_wait);
                                            pthread_mutex_lock(&mx_concent);
                                            cad_wait_ms = cad_wait;
                                            cad_retry++;
                                        } else {
                                            /* Max retries exhausted — force TX, inhibit stays ON */
                                            MSG("WARNING: [jit] CAD still active after %d retries "
                                                "on rf_chain %d (freq=%u Hz, SF%u, tx_nf=%d dBm) "
                                                "— FORCING TX\n",
                                                CAD_MAX_RETRIES, i, pkt.freq_hz,
                                                pkt.datarate, first_noisefloor);
                                            cad_clear = true; /* force through */
                                            extra.cad_detected = true;
                                            extra.cad_retries = (uint8_t)cad_retry;
                                            extra.tx_noisefloor = (cad_result.rssi_dbm > -128) ? cad_result.rssi_dbm : first_noisefloor;
                                            extra.cad_reason = "forced_after_retries";
                                            break;
                                        }
                                    }
                                    extra.lbt_retries = lbt_retries_in_cad;
                                    gettimeofday(&cad_t1, NULL);
                                    extra.cad_duration_ms = (uint32_t)((cad_t1.tv_sec - cad_t0.tv_sec) * 1000 + (cad_t1.tv_usec - cad_t0.tv_usec) / 1000);
                                    /* Post-CAD: SX1261 is already in STDBY after CAD exit.
                                       No additional cleanup needed. */
                                    if (!cad_clear) {
                                        clbt_blocked = true;
                                    }
                                } else {
                                    MSG_DEBUG(DEBUG_PKT_FWD, "Pre-TX: unsupported BW 0x%02x for CAD, "
                                        "skipping CAD check\n", pkt.bandwidth);
                                    extra.cad_reason = "unsupported_bw";
                                }
                            }

                            /* --- WM1303: HAL LBT takes over here ---
                               The real LBT decision happens inside lgw_send()
                               (via lgw_lbt_start/tx_status on the SX1261 using
                               the per-channel rssi_target_dbm configured at
                               startup). Pre-TX extra fields are populated from
                               the HAL LBT config snapshot; lbt_pass is set
                               from the lgw_send() return value below. */
                            hal_lbt_lookup(pkt.freq_hz, pkt.bandwidth,
                                           &extra.lbt_enabled,
                                           &extra.lbt_threshold_dbm);
                            extra.lbt_rssi_dbm = -128; /* unknown until lgw_send confirms a result */
                        }
                        /* --- Ensure SX1261 is NOT in RX mode before TX --- */
                        /* The mandatory pre-TX CAD scan leaves the SX1261 in standby
                           with tx_inhibit_rx=true. As an additional safety net, we
                           explicitly stop any LoRa RX that may have been restarted
                           by other threads. This prevents the FEM from routing to
                           LNA (receive path) which would cause silent TX failures. */
                        sx1261_lora_rx_stop();
                        MSG("INFO: [jit] SX1261 LoRa RX stopped before lgw_send on rf_chain %d\n", i);
                        /* WM1303: Force IMMEDIATE TX mode after CAD scan.
                           The CAD scan adds significant delay (up to 500ms with
                           GPIO reset + PRAM reload). In TIMESTAMPED mode, the
                           original count_us is now in the past, causing the
                           SX1302 TX FSM to stay in TX_SCHEDULED (0x91) forever
                           because the timer trigger never fires.
                           IMMEDIATE mode sends the packet right away — the CAD
                           scan already confirmed the channel is clear. */
                        pkt.tx_mode = IMMEDIATE;
                        MSG("INFO: [jit] TX mode forced to IMMEDIATE after CAD on rf_chain %d\n", i);
                        result = lgw_send(&pkt);
                        hal_lbt_capture_result(&extra, result);
                        /* Do NOT restart LoRa RX here — SX1302 TX is in progress.
                           The FEM must stay in TX/neutral mode for the full airtime.
                           We release the mutex but keep the inhibit active. */
                        gettimeofday(&lgw_forensic_last_tx, NULL); /* forensic: track TX event */
                        pthread_mutex_unlock(&mx_concent); /* free concentrator ASAP */
                        if (result != LGW_HAL_SUCCESS) {
                            /* TX failed — release inhibit and restart RX immediately */
                            pthread_mutex_lock(&mx_concent);
                            sx1261_set_tx_inhibit_rx(false);
                            sx1261_lora_rx_restart_light();
                            pthread_mutex_unlock(&mx_concent);
                            MSG("INFO: [jit] TX failed, LoRa RX restarted on rf_chain %d\n", i);
                            pthread_mutex_lock(&mx_meas_dw);
                            meas_nb_tx_fail += 1;
                            pthread_mutex_unlock(&mx_meas_dw);
                            MSG("WARNING: [jit] lgw_send failed on rf_chain %d\n", i);
                            /* Only the dedicated HAL return code confirms an LBT block. */
                            if (result == LGW_LBT_NOT_ALLOWED) {
                                extra.tx_result = "blocked";
                                MSG("INFO: [jit] HAL LBT BLOCKED TX on rf_chain %d (freq=%u Hz, thr=%d dBm)\n",
                                    i, pkt.freq_hz, extra.lbt_threshold_dbm);
                            } else {
                                extra.tx_result = "send_failed";
                            }
                            {
                                uint8_t _tk_h = 0, _tk_l = 0;
                                if (tx_ack_token_claim((uint8_t)i, &pkt, &_tk_h, &_tk_l)) {
                                    send_tx_ack(_tk_h, _tk_l, JIT_ERROR_OK, 0, &extra);
                                }
                            }
                            continue;
                        } else {
                            pthread_mutex_lock(&mx_meas_dw);
                            meas_nb_tx_ok += 1;
                            pthread_mutex_unlock(&mx_meas_dw);
                            /* Note: TX timestamp tracking removed — was used for L0 burst detection
                               (AGC-reload bursts), no longer needed after AGC reload removal. */
                            MSG_DEBUG(DEBUG_PKT_FWD, "lgw_send done on rf_chain %d: count_us=%u\n", i, pkt.count_us);
                            /* WM1303: emit post-TX TX_ACK with CAD/LBT results */
                            extra.tx_result = "sent";
                            {
                                uint8_t _tk_h = 0, _tk_l = 0;
                                if (tx_ack_token_claim((uint8_t)i, &pkt, &_tk_h, &_tk_l)) {
                                    send_tx_ack(_tk_h, _tk_l, JIT_ERROR_OK, 0, &extra);
                                }
                            }

                            /* Compute estimated airtime for this packet */
                            uint32_t est_airtime_ms = 500; /* default fallback */
                            if (pkt.bandwidth == BW_125KHZ) {
                                switch (pkt.datarate) {
                                    case DR_LORA_SF7:  est_airtime_ms = 50 + pkt.size * 2;   break;
                                    case DR_LORA_SF8:  est_airtime_ms = 100 + pkt.size * 3;  break;
                                    case DR_LORA_SF9:  est_airtime_ms = 200 + pkt.size * 5;  break;
                                    case DR_LORA_SF10: est_airtime_ms = 400 + pkt.size * 9;  break;
                                    case DR_LORA_SF11: est_airtime_ms = 800 + pkt.size * 17; break;
                                    case DR_LORA_SF12: est_airtime_ms = 1600 + pkt.size * 33; break;
                                    default: est_airtime_ms = 500; break;
                                }
                            } else if (pkt.bandwidth == BW_250KHZ) {
                                switch (pkt.datarate) {
                                    case DR_LORA_SF7:  est_airtime_ms = 30 + pkt.size * 1;   break;
                                    case DR_LORA_SF8:  est_airtime_ms = 50 + pkt.size * 2;   break;
                                    default: est_airtime_ms = 250; break;
                                }
                            } else if (pkt.bandwidth == BW_500KHZ) {
                                est_airtime_ms = 25 + pkt.size * 1;
                            } else {
                                /* BW_62K5HZ (Channel E) */
                                switch (pkt.datarate) {
                                    case DR_LORA_SF7:  est_airtime_ms = 100 + pkt.size * 4;  break;
                                    case DR_LORA_SF8:  est_airtime_ms = 200 + pkt.size * 7;  break;
                                    case DR_LORA_SF9:  est_airtime_ms = 400 + pkt.size * 13; break;
                                    default: est_airtime_ms = 1000; break;
                                }
                            }


                            /* Event-driven SX1261 RX restart (Option D):
                               Poll lgw_status(TX_STATUS) until TX_FREE rather
                               than blind usleep(airtime+20ms). Same logic as the
                               [imme] path. Hard-cap timeout prevents hang. */
                            {
                                uint8_t _tx_stat = 0;
                                uint32_t _poll_count = 0;
                                uint32_t _max_wait_ms = est_airtime_ms + 100;
                                uint32_t _poll_interval_us = 2000; /* 2ms */
                                uint32_t _max_polls = (_max_wait_ms * 1000) / _poll_interval_us;
                                int _poll_err = 0;
                                bool _tx_done = false;
                                usleep(5000);
                                while (_poll_count < _max_polls) {
                                    pthread_mutex_lock(&mx_concent);
                                    _poll_err = lgw_status((uint8_t)i, TX_STATUS, &_tx_stat);
                                    pthread_mutex_unlock(&mx_concent);
                                    if (_poll_err == LGW_HAL_SUCCESS && _tx_stat == TX_FREE) {
                                        _tx_done = true;
                                        usleep(5000);
                                        break;
                                    }
                                    usleep(_poll_interval_us);
                                    _poll_count++;
                                }
                                if (!_tx_done) {
                                    MSG("WARNING: [jit] TX_DONE polling timed out after %u ms on rf_chain %d (airtime estimate was %u)\n",
                                        _max_wait_ms, i, est_airtime_ms);
                                }
                                pthread_mutex_lock(&mx_concent);
                                sx1261_set_tx_inhibit_rx(false);
                                sx1261_lora_rx_restart_light();
                                pthread_mutex_unlock(&mx_concent);
                                custom_lbt_rx_restart_pending = false;
                                MSG("INFO: [jit] SX1261 LoRa RX restarted (TX_DONE polled, rf_chain %d, %u polls, airtime_est=%u ms)\n",
                                    i, _poll_count, est_airtime_ms);
                            }
                        }
                    } else {
                        MSG("ERROR: jit_dequeue failed on rf_chain %d with %d\n", i, jit_result);
                    }
                }
            } else if (jit_result == JIT_ERROR_EMPTY) {
                /* Do nothing, it can happen */
            } else {
                MSG("ERROR: jit_peek failed on rf_chain %d with %d\n", i, jit_result);
            }
        }
    }

    MSG("\nINFO: End of JIT thread\n");
}

/* -------------------------------------------------------------------------- */
/* --- THREAD 4: PARSE GPS MESSAGE AND KEEP GATEWAY IN SYNC ----------------- */

static void gps_process_sync(void) {
    struct timespec gps_time;
    struct timespec utc;
    uint32_t trig_tstamp; /* concentrator timestamp associated with PPM pulse */
    int i = lgw_gps_get(&utc, &gps_time, NULL, NULL);

    /* get GPS time for synchronization */
    if (i != LGW_GPS_SUCCESS) {
        MSG("WARNING: [gps] could not get GPS time from GPS\n");
        return;
    }

    /* get timestamp captured on PPM pulse  */
    pthread_mutex_lock(&mx_concent);
    i = lgw_get_trigcnt(&trig_tstamp);
    pthread_mutex_unlock(&mx_concent);
    if (i != LGW_HAL_SUCCESS) {
        MSG("WARNING: [gps] failed to read concentrator timestamp\n");
        return;
    }

    /* try to update time reference with the new GPS time & timestamp */
    pthread_mutex_lock(&mx_timeref);
    i = lgw_gps_sync(&time_reference_gps, trig_tstamp, utc, gps_time);
    pthread_mutex_unlock(&mx_timeref);
    if (i != LGW_GPS_SUCCESS) {
        MSG("WARNING: [gps] GPS out of sync, keeping previous time reference\n");
    }
}

static void gps_process_coords(void) {
    /* position variable */
    struct coord_s coord;
    struct coord_s gpserr;
    int    i = lgw_gps_get(NULL, NULL, &coord, &gpserr);

    /* update gateway coordinates */
    pthread_mutex_lock(&mx_meas_gps);
    if (i == LGW_GPS_SUCCESS) {
        gps_coord_valid = true;
        meas_gps_coord = coord;
        meas_gps_err = gpserr;
        // TODO: report other GPS statistics (typ. signal quality & integrity)
    } else {
        gps_coord_valid = false;
    }
    pthread_mutex_unlock(&mx_meas_gps);
}

void thread_gps(void) {
    /* serial variables */
    char serial_buff[128]; /* buffer to receive GPS data */
    size_t wr_idx = 0;     /* pointer to end of chars in buffer */

    /* variables for PPM pulse GPS synchronization */
    enum gps_msg latest_msg; /* keep track of latest NMEA message parsed */

    /* initialize some variables before loop */
    memset(serial_buff, 0, sizeof serial_buff);

    while (!exit_sig && !quit_sig) {
        size_t rd_idx = 0;
        size_t frame_end_idx = 0;

        /* blocking non-canonical read on serial port */
        ssize_t nb_char = read(gps_tty_fd, serial_buff + wr_idx, LGW_GPS_MIN_MSG_SIZE);
        if (nb_char <= 0) {
            MSG("WARNING: [gps] read() returned value %zd\n", nb_char);
            continue;
        }
        wr_idx += (size_t)nb_char;

        /*******************************************
         * Scan buffer for UBX/NMEA sync chars and *
         * attempt to decode frame if one is found *
         *******************************************/
        while (rd_idx < wr_idx) {
            size_t frame_size = 0;

            /* Scan buffer for UBX sync char */
            if (serial_buff[rd_idx] == (char)LGW_GPS_UBX_SYNC_CHAR) {

                /***********************
                 * Found UBX sync char *
                 ***********************/
                latest_msg = lgw_parse_ubx(&serial_buff[rd_idx], (wr_idx - rd_idx), &frame_size);

                if (frame_size > 0) {
                    if (latest_msg == INCOMPLETE) {
                        /* UBX header found but frame appears to be missing bytes */
                        frame_size = 0;
                    } else if (latest_msg == INVALID) {
                        /* message header received but message appears to be corrupted */
                        MSG("WARNING: [gps] could not get a valid message from GPS (no time)\n");
                        frame_size = 0;
                    } else if (latest_msg == UBX_NAV_TIMEGPS) {
                        gps_process_sync();
                    }
                }
            } else if (serial_buff[rd_idx] == (char)LGW_GPS_NMEA_SYNC_CHAR) {
                /************************
                 * Found NMEA sync char *
                 ************************/
                /* scan for NMEA end marker (LF = 0x0a) */
                char* nmea_end_ptr = memchr(&serial_buff[rd_idx],(int)0x0a, (wr_idx - rd_idx));

                if(nmea_end_ptr) {
                    /* found end marker */
                    frame_size = nmea_end_ptr - &serial_buff[rd_idx] + 1;
                    latest_msg = lgw_parse_nmea(&serial_buff[rd_idx], frame_size);

                    if(latest_msg == INVALID || latest_msg == UNKNOWN) {
                        /* checksum failed */
                        frame_size = 0;
                    } else if (latest_msg == NMEA_RMC) { /* Get location from RMC frames */
                        gps_process_coords();
                    }
                }
            }

            if (frame_size > 0) {
                /* At this point message is a checksum verified frame
                   we're processed or ignored. Remove frame from buffer */
                rd_idx += frame_size;
                frame_end_idx = rd_idx;
            } else {
                rd_idx++;
            }
        } /* ...for(rd_idx = 0... */

        if (frame_end_idx) {
          /* Frames have been processed. Remove bytes to end of last processed frame */
          memmove(serial_buff, &serial_buff[frame_end_idx], wr_idx - frame_end_idx);
          wr_idx -= frame_end_idx;
        } /* ...for(rd_idx = 0... */

        /* Prevent buffer overflow */
        if ((sizeof(serial_buff) - wr_idx) < LGW_GPS_MIN_MSG_SIZE) {
            memmove(serial_buff, &serial_buff[LGW_GPS_MIN_MSG_SIZE], wr_idx - LGW_GPS_MIN_MSG_SIZE);
            wr_idx -= LGW_GPS_MIN_MSG_SIZE;
        }
    }
    MSG("\nINFO: End of GPS thread\n");
}

/* -------------------------------------------------------------------------- */
/* --- THREAD 5: CHECK TIME REFERENCE AND CALCULATE XTAL CORRECTION --------- */

void thread_valid(void) {

    /* GPS reference validation variables */
    long gps_ref_age = 0;
    bool ref_valid_local = false;
    double xtal_err_cpy;

    /* variables for XTAL correction averaging */
    unsigned init_cpt = 0;
    double init_acc = 0.0;
    double x;

    /* correction debug */
    // FILE * log_file = NULL;
    // time_t now_time;
    // char log_name[64];

    /* initialization */
    // time(&now_time);
    // strftime(log_name,sizeof log_name,"xtal_err_%Y%m%dT%H%M%SZ.csv",localtime(&now_time));
    // log_file = fopen(log_name, "w");
    // setbuf(log_file, NULL);
    // fprintf(log_file,"\"xtal_correct\",\"XERR_INIT_AVG %u XERR_FILT_COEF %u\"\n", XERR_INIT_AVG, XERR_FILT_COEF); // DEBUG

    /* main loop task */
    while (!exit_sig && !quit_sig) {
        wait_ms(1000);

        /* calculate when the time reference was last updated */
        pthread_mutex_lock(&mx_timeref);
        gps_ref_age = (long)difftime(time(NULL), time_reference_gps.systime);
        if ((gps_ref_age >= 0) && (gps_ref_age <= GPS_REF_MAX_AGE)) {
            /* time ref is ok, validate and  */
            gps_ref_valid = true;
            ref_valid_local = true;
            xtal_err_cpy = time_reference_gps.xtal_err;
            //printf("XTAL err: %.15lf (1/XTAL_err:%.15lf)\n", xtal_err_cpy, 1/xtal_err_cpy); // DEBUG
        } else {
            /* time ref is too old, invalidate */
            gps_ref_valid = false;
            ref_valid_local = false;
        }
        pthread_mutex_unlock(&mx_timeref);

        /* manage XTAL correction */
        if (ref_valid_local == false) {
            /* couldn't sync, or sync too old -> invalidate XTAL correction */
            pthread_mutex_lock(&mx_xcorr);
            xtal_correct_ok = false;
            xtal_correct = 1.0;
            pthread_mutex_unlock(&mx_xcorr);
            init_cpt = 0;
            init_acc = 0.0;
        } else {
            if (init_cpt < XERR_INIT_AVG) {
                /* initial accumulation */
                init_acc += xtal_err_cpy;
                ++init_cpt;
            } else if (init_cpt == XERR_INIT_AVG) {
                /* initial average calculation */
                pthread_mutex_lock(&mx_xcorr);
                xtal_correct = (double)(XERR_INIT_AVG) / init_acc;
                //printf("XERR_INIT_AVG=%d, init_acc=%.15lf\n", XERR_INIT_AVG, init_acc);
                xtal_correct_ok = true;
                pthread_mutex_unlock(&mx_xcorr);
                ++init_cpt;
                // fprintf(log_file,"%.18lf,\"average\"\n", xtal_correct); // DEBUG
            } else {
                /* tracking with low-pass filter */
                x = 1 / xtal_err_cpy;
                pthread_mutex_lock(&mx_xcorr);
                xtal_correct = xtal_correct - xtal_correct/XERR_FILT_COEF + x/XERR_FILT_COEF;
                pthread_mutex_unlock(&mx_xcorr);
                // fprintf(log_file,"%.18lf,\"track\"\n", xtal_correct); // DEBUG
            }
        }

        //printf("Time ref: %s, XTAL correct: %s (%.15lf)\n", ref_valid_local?"valid":"invalid", xtal_correct_ok?"valid":"invalid", xtal_correct); // DEBUG
    }
    MSG("\nINFO: End of validation thread\n");
}

/* -------------------------------------------------------------------------- */
/* --- THREAD 6: BACKGROUND SPECTRAL SCAN                           --------- */

void thread_spectral_scan(void) {
    FILE *dbgf = fopen("/tmp/spectral_debug.log", "w");
    if (dbgf) { fprintf(dbgf, "spectral scan thread started\n"); fflush(dbgf); }
    int i, x;
    uint32_t freq_hz = spectral_scan_params.freq_hz_start;
    uint32_t freq_hz_stop = spectral_scan_params.freq_hz_start + spectral_scan_params.nb_chan * 200E3;
    int16_t levels[LGW_SPECTRAL_SCAN_RESULT_SIZE];
    uint16_t results[LGW_SPECTRAL_SCAN_RESULT_SIZE];
    struct timeval tm_start;
    lgw_spectral_scan_status_t status;
    uint8_t tx_status = TX_FREE;
    bool spectral_scan_started;
    bool exit_thread = false;

    /* --- Accumulator for JSON output (one full sweep) --- */
    /* Max 64 channels in a sweep (covers up to 12.8 MHz range) */
    #define SPECTRAL_MAX_CHAN 64
    static struct {
        uint32_t freq_hz;
        double   rssi_sum;
        double   rssi_min;
        double   rssi_max;
        uint32_t sample_count;
    } sweep_acc[SPECTRAL_MAX_CHAN];
    int sweep_idx = 0;
    int sweep_total = (int)spectral_scan_params.nb_chan;
    if (sweep_total > SPECTRAL_MAX_CHAN) sweep_total = SPECTRAL_MAX_CHAN;
    memset(sweep_acc, 0, sizeof(sweep_acc));

    printf("INFO: Spectral scan thread started (freq_start=%u, freq_stop=%u, nb_chan=%d, nb_scan=%d)\n",
           spectral_scan_params.freq_hz_start, (uint32_t)freq_hz_stop,
           spectral_scan_params.nb_chan, spectral_scan_params.nb_scan);

    /* Load CAD channel configuration */
    cad_read_config();


    /* main loop task */
    while (!exit_sig && !quit_sig) {
        /* Pace the scan thread (1 sec min), and avoid waiting several seconds when exit */
        for (i = 0; i < (int)(spectral_scan_params.pace_s ? spectral_scan_params.pace_s : 1); i++) {
            if (exit_sig || quit_sig) {
                exit_thread = true;
                break;
            }
            wait_ms(1000);
        }
        if (exit_thread == true) {
            break;
        }

        spectral_scan_started = false;
        bool channel_recorded = false; /* BUGFIX: track whether sweep_acc got a real or sentinel entry this iter */

        /* Start spectral scan only when TX is free and recent RX airtime guard has elapsed */
        int scan_ready = 0;
        int retries = 0;
        int32_t raw0 = -1, raw1 = -1;
        uint64_t now_ms;
        uint64_t last_rx_ms;
        uint32_t last_rx_toa_ms = 0;
        uint32_t rx_guard_ms;
        static int deferred_count = 0; /* track consecutive deferrals across loop iterations */

        while ((!exit_sig && !quit_sig) && (retries < 50)) {
            now_ms = spectral_now_ms();
            last_rx_ms = spectral_last_rx_ms;
            last_rx_toa_ms = spectral_last_rx_toa_ms;
            rx_guard_ms = last_rx_toa_ms + 50; /* airtime-aware guard + margin */

            pthread_mutex_lock(&mx_concent);
            lgw_reg_r(SX1302_REG_TX_TOP_TX_FSM_STATUS_TX_STATUS(0), &raw0);
            lgw_reg_r(SX1302_REG_TX_TOP_TX_FSM_STATUS_TX_STATUS(1), &raw1);
            pthread_mutex_unlock(&mx_concent);

            if (((raw0 & 0xFF) == 0x80) && ((raw1 & 0xFF) == 0x80) &&
                ((last_rx_ms == 0) || ((now_ms - last_rx_ms) >= rx_guard_ms))) {
                scan_ready = 1;
                deferred_count = 0; /* reset on success */
                break;
            }
            retries++;
            wait_ms(10);
        }

        if (!scan_ready) {
            deferred_count++;
            printf("INFO: spectral scan deferred (tx0=0x%02X tx1=0x%02X last_rx_toa=%ums retries=%d deferred_count=%d)\n",
                   raw0 & 0xFF, raw1 & 0xFF, last_rx_toa_ms, retries, deferred_count);
            fflush(stdout);

            /* Recovery: after 3 consecutive deferrals, force SX1261 reinit and start scan anyway.
             * The TX FSM being stuck at 0x91 is likely caused by CAD RF coupling.
             * The SX1302 is not actually transmitting, so it's safe to proceed. */
            if (deferred_count >= 3) {
                printf("WARNING: spectral scan stuck for %d cycles, attempting SX1261 recovery + forced scan start\n", deferred_count);
                fflush(stdout);

                pthread_mutex_lock(&mx_concent);

                /* Reinit SX1261: setup (STDBY) + set_rx_params (FSK mode for spectral scan) */
                int reinit_err = sx1261_setup();
                if (reinit_err != 0) {
                    printf("ERROR: spectral scan recovery: sx1261_setup failed\n");
                } else {
                    reinit_err = sx1261_set_rx_params(freq_hz, BW_125KHZ);
                    if (reinit_err != 0) {
                        printf("ERROR: spectral scan recovery: sx1261_set_rx_params failed\n");
                    } else {
                        printf("INFO: spectral scan recovery: SX1261 reinit OK, forcing scan start\n");
                    }
                }

                /* Force start spectral scan regardless of TX FSM status */
                x = lgw_spectral_scan_start(freq_hz, spectral_scan_params.nb_scan);
                gettimeofday(&lgw_forensic_last_spectral, NULL); /* forensic: track spectral scan */
                spectral_scan_started = true;
                printf("INFO: spectral scan FORCED start on freq=%u Hz (chan %d/%d), sweep_idx=%d (recovery after %d deferrals)\n",
                       freq_hz, sweep_idx+1, sweep_total, sweep_idx, deferred_count);
                fflush(stdout);

                pthread_mutex_unlock(&mx_concent);
                deferred_count = 0; /* reset after recovery attempt */
            } else {
                continue;
            }
        }

        if (retries > 0) {
            printf("INFO: spectral scan waited %d ms for TX/RX idle window\n", retries * 10);
            fflush(stdout);
        }

        pthread_mutex_lock(&mx_concent);
        x = lgw_spectral_scan_start(freq_hz, spectral_scan_params.nb_scan);
        spectral_scan_started = true;
        gettimeofday(&lgw_forensic_last_spectral, NULL); /* forensic: track spectral scan */
        printf("INFO: spectral scan started on freq=%u Hz (chan %d/%d), sweep_idx=%d\n", freq_hz, sweep_idx+1, sweep_total, sweep_idx);
        fflush(stdout);
        pthread_mutex_unlock(&mx_concent);

        if (spectral_scan_started == true) {
            /* Wait for scan to be completed */
            status = LGW_SPECTRAL_SCAN_STATUS_UNKNOWN;
            timeout_start(&tm_start);
            do {
                /* handle timeout */
                if (timeout_check(tm_start, 2000) != 0) {
                    printf("ERROR: %s: TIMEOUT on Spectral Scan — performing full SX1261 recovery\n", __FUNCTION__);
                    /* Full recovery: standby first to clear any stuck state, then restart RX */
                    pthread_mutex_lock(&mx_concent);
                    {
                        uint8_t _stby_buf[1] = { 0x00 }; /* STDBY_RC */
                        sx1261_reg_w(SX1261_SET_STANDBY, _stby_buf, 1);
                        wait_ms(2); /* Allow SX1261 to settle in standby */
                    }
                    sx1261_lora_rx_restart_light();
                    pthread_mutex_unlock(&mx_concent);
                    printf("INFO: %s: SX1261 recovered after scan timeout (standby → RX restart)\n", __FUNCTION__);
                    if (dbgf) { fprintf(dbgf, "LoRa RX restart after TIMEOUT (full recovery)\n"); fflush(dbgf); }
                    fflush(stdout);
                    break;  /* do while */
                }

                /* get spectral scan status */
                pthread_mutex_lock(&mx_concent);
                x = lgw_spectral_scan_get_status(&status);
                pthread_mutex_unlock(&mx_concent);
                if (x != 0) {
                    printf("ERROR: spectral scan status read failed — performing SX1261 recovery\n");
                    /* Recovery: the SX1261 state may be corrupted, force standby + RX restart */
                    pthread_mutex_lock(&mx_concent);
                    {
                        uint8_t _stby_buf[1] = { 0x00 }; /* STDBY_RC */
                        sx1261_reg_w(SX1261_SET_STANDBY, _stby_buf, 1);
                        wait_ms(2);
                    }
                    sx1261_lora_rx_restart_light();
                    pthread_mutex_unlock(&mx_concent);
                    printf("INFO: SX1261 recovered after status read failure (standby → RX restart)\n");
                    if (dbgf) { fprintf(dbgf, "LoRa RX restart after status_failed (full recovery)\n"); fflush(dbgf); }
                    fflush(stdout);
                    break; /* do while */
                }

                /* wait a bit before checking status again */
                wait_ms(10);
            } while (status != LGW_SPECTRAL_SCAN_STATUS_COMPLETED && status != LGW_SPECTRAL_SCAN_STATUS_ABORTED);

            printf("INFO: spectral scan status loop exited: status=%d (0=none,1=on_going,2=aborted,3=completed,4=unknown)\n", status);
            if (dbgf) { fprintf(dbgf, "status_loop_exit status=%d\n", status); fflush(dbgf); }
            fflush(stdout);

            if (status == LGW_SPECTRAL_SCAN_STATUS_COMPLETED) {
                /* Get spectral scan results */
                memset(levels, 0, sizeof levels);
                memset(results, 0, sizeof results);
                printf("DEBUG: about to call lgw_spectral_scan_get_results for freq=%u\n", freq_hz);
                if (dbgf) { fprintf(dbgf, "about to call get_results freq=%u status=%d\n", freq_hz, status); fflush(dbgf); }
                fflush(stdout);
                pthread_mutex_lock(&mx_concent);
                x = lgw_spectral_scan_get_results(levels, results);
                pthread_mutex_unlock(&mx_concent);
                printf("DEBUG: lgw_spectral_scan_get_results returned x=%d\n", x);
                if (dbgf) { fprintf(dbgf, "get_results returned x=%d\n", x); fflush(dbgf); }
                fflush(stdout);
                if (x != 0) {
                    /* BUGFIX: previously 'continue' here retried the same freq forever.
                     * Now: log, restore RX, leave channel_recorded=false so sentinel is
                     * filled in the common sweep-progression block below, and fall
                     * through to advance freq_hz. */
                    printf("ERROR: spectral scan get results failed (x=%d) — sentinel will be recorded for freq=%u\n", x, freq_hz);
                    pthread_mutex_lock(&mx_concent);
                    sx1261_lora_rx_restart_light();
                    pthread_mutex_unlock(&mx_concent);
                } else {
                    /* print raw results to debug file (avoid blocking stdout) */
                    if (dbgf) {
                        fprintf(dbgf, "SPECTRAL SCAN - %u Hz: ", freq_hz);
                        for (i = 0; i < LGW_SPECTRAL_SCAN_RESULT_SIZE; i++) {
                            fprintf(dbgf, "%u ", results[i]);
                        }
                        fprintf(dbgf, "\n");
                        fflush(dbgf);
                    }

                    /* Compute RSSI statistics from histogram */
                    double rssi_sum = 0.0;
                    double rssi_min = 0.0;
                    double rssi_max = -200.0;
                    uint32_t total_samples = 0;
                    bool first_sample = true;
                    for (i = 0; i < LGW_SPECTRAL_SCAN_RESULT_SIZE; i++) {
                        if (results[i] > 0) {
                            double rssi_val = (double)levels[i];
                            rssi_sum += rssi_val * results[i];
                            total_samples += results[i];
                            if (first_sample || rssi_val < rssi_min) rssi_min = rssi_val;
                            if (first_sample || rssi_val > rssi_max) rssi_max = rssi_val;
                            first_sample = false;
                        }
                    }
                    /* === BUGFIX v2.3.2: Sanity check on histogram data ===
                     * Occasionally lgw_spectral_scan_get_results() returns
                     * success (x=0) but with uninitialized/stale memory in
                     * results[], yielding wildly wrong sample counts (observed:
                     * ~911k samples with all 33 bins containing 20k-60k each).
                     * A healthy scan always sums to exactly nb_scan samples.
                     * If total_samples exceeds 2x nb_scan, treat the data as
                     * corrupted — do NOT record it and fall through to the
                     * sentinel path below. */
                    uint32_t expected_samples = (uint32_t)spectral_scan_params.nb_scan;
                    if (total_samples > expected_samples * 2 || total_samples == 0) {
                        if (total_samples > expected_samples * 2) {
                            printf("WARNING: spectral scan histogram corrupted for freq=%u (total_samples=%u, expected=%u) — discarding, sentinel will be recorded\n",
                                   freq_hz, total_samples, expected_samples);
                            if (dbgf) { fprintf(dbgf, "histogram_corrupted freq=%u total=%u expected=%u\n", freq_hz, total_samples, expected_samples); fflush(dbgf); }
                            fflush(stdout);
                        }
                        /* Do not set channel_recorded — sentinel block will fill below */
                    } else if (sweep_idx < sweep_total) {
                        /* Store in sweep accumulator — valid data */
                        sweep_acc[sweep_idx].freq_hz = freq_hz;
                        if (total_samples > 0) {
                            sweep_acc[sweep_idx].rssi_sum = rssi_sum / total_samples;
                        } else {
                            sweep_acc[sweep_idx].rssi_sum = -120.0;
                        }
                        sweep_acc[sweep_idx].rssi_min = first_sample ? -120.0 : rssi_min;
                        sweep_acc[sweep_idx].rssi_max = first_sample ? -120.0 : rssi_max;
                        sweep_acc[sweep_idx].sample_count = total_samples;
                        sweep_idx++;
                        channel_recorded = true; /* BUGFIX: mark that real data was recorded */
                    }

                    /* Restore LoRa RX between scan channels to minimize Channel E downtime */
                    pthread_mutex_lock(&mx_concent);
                    sx1261_lora_rx_restart_light();
                    pthread_mutex_unlock(&mx_concent);
                    if (dbgf) { fprintf(dbgf, "per-channel LoRa RX restart after chan %d\n", sweep_idx); fflush(dbgf); }
                }
            } else if (status == LGW_SPECTRAL_SCAN_STATUS_ABORTED) {
                printf("INFO: %s: spectral scan has been aborted\n", __FUNCTION__);
                /* Restore LoRa RX after aborted scan */
                pthread_mutex_lock(&mx_concent);
                sx1261_lora_rx_restart_light();
                pthread_mutex_unlock(&mx_concent);
                if (dbgf) { fprintf(dbgf, "LoRa RX restart after ABORTED scan\n"); fflush(dbgf); }
                fflush(stdout);
                /* BUGFIX: fall through to sentinel fill + freq advance below */
            } else {
                printf("ERROR: %s: spectral scan status unexpected 0x%02X — performing SX1261 recovery\n", __FUNCTION__, status);
                /* Recovery: unexpected status means the SX1261 is in an unknown state.
                   Force standby to clear any stuck operation, then restart LoRa RX. */
                pthread_mutex_lock(&mx_concent);
                {
                    uint8_t _stby_buf[1] = { 0x00 }; /* STDBY_RC */
                    sx1261_reg_w(SX1261_SET_STANDBY, _stby_buf, 1);
                    wait_ms(2);
                }
                sx1261_lora_rx_restart_light();
                pthread_mutex_unlock(&mx_concent);
                printf("INFO: SX1261 recovered after unexpected status (standby → RX restart)\n");
                if (dbgf) { fprintf(dbgf, "LoRa RX restart after unexpected status 0x%02X\n", status); fflush(dbgf); }
                fflush(stdout);
                /* BUGFIX: fall through to sentinel fill + freq advance below */
            }

            /* === BUGFIX v2.3.2: COMMON sweep-progression logic ===
             * Previously, all error paths (TIMEOUT in do-while, status-read-fail,
             * get_results-fail, ABORTED, unexpected status) skipped the freq_hz
             * advance, causing the sweep to get stuck retrying the same channel
             * forever. Now we always advance here, recording a sentinel in
             * sweep_acc for any channel that didn't get real data. This
             * guarantees the sweep completes within bounded time regardless of
             * individual channel failures.
             *
             * Design principles check:
             *  - RX availability: improved (sweep can't get stuck hogging SX1261)
             *  - TX latency: unchanged (sweep thread doesn't block TX)
             *  - No workaround: this is a real fix for a real logic bug */

            /* Record sentinel for this channel if no real data was captured */
            if (!channel_recorded && sweep_idx < sweep_total) {
                sweep_acc[sweep_idx].freq_hz = freq_hz;
                sweep_acc[sweep_idx].rssi_sum = -127.0;
                sweep_acc[sweep_idx].rssi_min = -127.0;
                sweep_acc[sweep_idx].rssi_max = -127.0;
                sweep_acc[sweep_idx].sample_count = 0;
                sweep_idx++;
                printf("INFO: spectral scan recorded sentinel for freq=%u Hz (status=%d, no valid data)\n", freq_hz, status);
                if (dbgf) { fprintf(dbgf, "sentinel recorded for freq=%u status=%d\n", freq_hz, status); fflush(dbgf); }
                fflush(stdout);
            }

            /* Advance to next frequency (common to success and error paths) */
            freq_hz += 200000; /* 200kHz channels */

            if (freq_hz >= freq_hz_stop) {
                /* Full sweep completed (with or without sentinels) */

                /* --- Run hardware CAD on configured channels --- */
                if (cad_config.enable) {
                    /* Re-read config each sweep (allows runtime changes) */
                    cad_read_config();

                    for (i = 0; i < cad_config.nb_channels; i++) {
                        /* Acquire concentrator mutex for SX1261 access */
                        pthread_mutex_lock(&mx_concent);

                        /* Check TX not in progress before CAD */
                        int tx_busy = 0;
                        int rf_chain;
                        for (rf_chain = 0; rf_chain < LGW_RF_CHAIN_NB; rf_chain++) {
                            if (tx_enable[rf_chain] == true) {
                                x = lgw_status((uint8_t)rf_chain, TX_STATUS, &tx_status);
                                if (x == LGW_HAL_SUCCESS && (tx_status == TX_SCHEDULED || tx_status == TX_EMITTING)) {
                                    tx_busy = 1;
                                    break;
                                }
                            }
                        }

                        if (tx_busy) {
                            /* TX active — skip CAD for this channel */
                            pthread_mutex_unlock(&mx_concent);
                            cad_results[i].detected = false;
                            cad_results[i].rssi_dbm = -128;
                            cad_results[i].status = 2; /* skipped */
                            printf("INFO: CAD skipped on %s (TX busy)\n", cad_config.channels[i].id);
                            continue;
                        }

                        /* Perform hardware CAD scan */
                        x = sx1261_cad_scan(
                            cad_config.channels[i].freq_hz,
                            cad_config.channels[i].sf,
                            cad_config.channels[i].bw,
                            &cad_results[i],
                            false); /* not a retry — always do full scan with noisefloor */

                        pthread_mutex_unlock(&mx_concent);

                        if (x != LGW_REG_SUCCESS) {
                            printf("ERROR: CAD scan failed on %s\n", cad_config.channels[i].id);
                            cad_results[i].status = 2;
                        } else {
                            printf("INFO: CAD %s: %s (tx_nf=%d, status=%u)\n",
                                   cad_config.channels[i].id,
                                   cad_results[i].detected ? "DETECTED" : "clear",
                                   cad_results[i].rssi_dbm,
                                   cad_results[i].status);
                        }
                    }
                }

                /* --- Write JSON results file (spectral + CAD) --- */
                FILE *fp = fopen("/tmp/pymc_spectral_results.json.tmp", "w");
                if (fp != NULL) {
                    fprintf(fp, "{\n  \"timestamp\": %ld,\n  \"channels\": {\n",
                            (long)time(NULL));
                    int written = 0;
                    for (i = 0; i < sweep_idx && i < sweep_total; i++) {
                        if (written > 0) fprintf(fp, ",\n");
                        fprintf(fp, "    \"%u\": {\"rssi_avg\": %.1f, \"rssi_min\": %.1f, "
                                    "\"rssi_max\": %.1f, \"samples\": %u}",
                                sweep_acc[i].freq_hz,
                                sweep_acc[i].rssi_sum,
                                sweep_acc[i].rssi_min,
                                sweep_acc[i].rssi_max,
                                sweep_acc[i].sample_count);
                        written++;
                    }
                    fprintf(fp, "\n  }");

                    /* Append CAD results if available */
                    if (cad_config.enable && cad_config.nb_channels > 0) {
                        fprintf(fp, ",\n  \"cad\": {\n");
                        for (i = 0; i < cad_config.nb_channels; i++) {
                            if (i > 0) fprintf(fp, ",\n");
                            fprintf(fp, "    \"%s\": {\"freq_hz\": %u, \"sf\": %u, "
                                        "\"detected\": %s, \"tx_noisefloor\": %d, \"status\": %u}",
                                    cad_config.channels[i].id,
                                    cad_config.channels[i].freq_hz,
                                    cad_config.channels[i].sf,
                                    cad_results[i].detected ? "true" : "false",
                                    cad_results[i].rssi_dbm,
                                    cad_results[i].status);
                        }
                        fprintf(fp, "\n  }");
                    }

                    fprintf(fp, "\n}\n");
                    fclose(fp);
                    /* Atomic rename to avoid partial reads */
                    rename("/tmp/pymc_spectral_results.json.tmp",
                           "/tmp/pymc_spectral_results.json");
                    printf("INFO: spectral sweep complete (%d channels%s), results written\n",
                           written,
                           cad_config.enable ? " + CAD" : "");
                    /* Restore SX1261 LoRa RX after spectral scan sweep */
                    pthread_mutex_lock(&mx_concent);
                    if (sx1261_lora_rx_active() == false) {
                        sx1261_lora_rx_restart_light();
                        printf("INFO: SX1261 LoRa RX restarted after spectral scan sweep\n");
                        fflush(stdout);
                    }
                    pthread_mutex_unlock(&mx_concent);
                } else {
                    printf("ERROR: could not open /tmp/pymc_spectral_results.json.tmp for writing\n");
                }

                /* Reset sweep accumulator */
                freq_hz = spectral_scan_params.freq_hz_start;
                sweep_idx = 0;
                memset(sweep_acc, 0, sizeof(sweep_acc));
            }
        }
    }
    if (dbgf != NULL) {
        fclose(dbgf);
    }
    printf("\nINFO: End of Spectral Scan thread\n");
}

/* --- EOF ------------------------------------------------------------------ */
