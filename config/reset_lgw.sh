#!/bin/sh
# GPIO reset script for WM1303 CoreCell
# BCM pins: reset=17, power=18, sx1261=5, ad5338r=13
# GPIO base offset: 512
#
# Usage:
#   reset_lgw.sh start         - Normal start (quick reset + power on)
#   reset_lgw.sh stop          - Power down and hold resets
#   reset_lgw.sh deep_reset    - Extended hardware drain (>60s power off)

set -eu

SX1302_RESET_PIN=529
SX1302_POWER_EN_PIN=530
SX1261_RESET_PIN=517
AD5338R_RESET_PIN=525

# Default drain time for deep_reset (seconds)
DRAIN_TIME=${2:-60}
case "$DRAIN_TIME" in
    ''|*[!0-9]*)
        echo "drain_seconds must be a non-negative integer" >&2
        exit 1
        ;;
esac

WAIT_GPIO() {
    sleep 0.1
}

init() {
    for pin in ${SX1302_RESET_PIN} ${SX1261_RESET_PIN} ${SX1302_POWER_EN_PIN} ${AD5338R_RESET_PIN}; do
        echo "${pin}" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
        echo "out" > /sys/class/gpio/gpio${pin}/direction; WAIT_GPIO
    done
}

power_down() {
    echo "CoreCell power OFF through GPIO${SX1302_POWER_EN_PIN} (BCM18)..."
    echo "0" > /sys/class/gpio/gpio${SX1302_POWER_EN_PIN}/value; WAIT_GPIO

    echo "SX1302 RESET asserted through GPIO${SX1302_RESET_PIN} (BCM17)..."
    echo "1" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 RESET asserted through GPIO${SX1261_RESET_PIN} (BCM5)..."
    echo "0" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R RESET asserted through GPIO${AD5338R_RESET_PIN} (BCM13)..."
    echo "0" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
}

power_up() {
    echo "Releasing resets..."
    echo "0" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
    sleep 0.5

    echo "CoreCell power enable through GPIO${SX1302_POWER_EN_PIN} (BCM18)..."
    echo "1" > /sys/class/gpio/gpio${SX1302_POWER_EN_PIN}/value; WAIT_GPIO
    sleep 0.5

    echo "CoreCell reset through GPIO${SX1302_RESET_PIN} (BCM17)..."
    echo "1" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 reset through GPIO${SX1261_RESET_PIN} (BCM5)..."
    echo "0" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R reset through GPIO${AD5338R_RESET_PIN} (BCM13)..."
    echo "0" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
}

reset() {
    echo "CoreCell power enable through GPIO${SX1302_POWER_EN_PIN} (BCM18)..."
    echo "1" > /sys/class/gpio/gpio${SX1302_POWER_EN_PIN}/value; WAIT_GPIO

    echo "CoreCell reset through GPIO${SX1302_RESET_PIN} (BCM17)..."
    echo "1" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 reset through GPIO${SX1261_RESET_PIN} (BCM5)..."
    echo "0" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R reset through GPIO${AD5338R_RESET_PIN} (BCM13)..."
    echo "0" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; WAIT_GPIO
}

term() {
    for pin in ${SX1302_RESET_PIN} ${SX1261_RESET_PIN} ${SX1302_POWER_EN_PIN} ${AD5338R_RESET_PIN}; do
        if [ -d /sys/class/gpio/gpio${pin} ]; then
            echo "${pin}" > /sys/class/gpio/unexport 2>/dev/null || true; WAIT_GPIO
        fi
    done
}

case "${1:-}" in
    start)
        term
        init
        reset
        sleep 1
        ;;
    stop)
        init
        power_down
        ;;
    deep_reset)
        echo "=== Extended hardware drain reset ==="
        echo "Initializing GPIOs..."
        init

        echo "Powering down all components..."
        power_down

        echo "Holding all resets for ${DRAIN_TIME} seconds to clear hardware state..."
        ELAPSED=0
        while [ $ELAPSED -lt $DRAIN_TIME ]; do
            REMAINING=$((DRAIN_TIME - ELAPSED))
            printf "\r  Draining... %d seconds remaining  " $REMAINING
            INTERVAL=10
            [ "$REMAINING" -ge "$INTERVAL" ] || INTERVAL=$REMAINING
            sleep "$INTERVAL"
            ELAPSED=$((ELAPSED + INTERVAL))
        done
        printf "\r  Drain complete (%d seconds)          \n" $DRAIN_TIME

        echo "Powering up with clean state..."
        power_up
        sleep 1

        echo "=== Hardware drain reset complete ==="
        ;;
    *)
        echo "Usage: $0 {start|stop|deep_reset} [drain_seconds]"
        echo "  start       - Normal start (quick reset + power on)"
        echo "  stop        - Power down and hold resets"
        echo "  deep_reset  - Extended power-off drain (default 60s)"
        exit 1
        ;;
esac
exit 0
