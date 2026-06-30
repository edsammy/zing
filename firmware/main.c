#include "pico/stdlib.h"
#include "hardware/i2c.h"
#include <stdio.h>

#define INA_ADDR        0x40

// INA228 register map
#define INA_REG_CONFIG     0x00
#define INA_REG_ADCCFG     0x01
#define INA_REG_SHUNTCAL   0x02
#define INA_REG_VBUS       0x05
#define INA_REG_CURRENT    0x07
#define INA_REG_POWER      0x08
#define INA_REG_ENERGY     0x09
#define INA_REG_CHARGE     0x0A

// I2C pins
#define I2C_PORT i2c0
#define I2C_SDA  16
#define I2C_SCL  17

// Shunt and measurement range
#define SHUNT_R    0.015f
#define MAX_I      10.0f

static float current_lsb;

// ---------------------------------------------------------------------------
// Low-level I2C helpers
// ---------------------------------------------------------------------------

static bool ina228_read(uint8_t reg, uint8_t *buf, size_t len) {
    if (i2c_write_blocking(I2C_PORT, INA_ADDR, &reg, 1, true) != 1)
        return false;
    if (i2c_read_blocking(I2C_PORT, INA_ADDR, buf, len, false) != (int)len)
        return false;
    return true;
}

static bool ina228_write16(uint8_t reg, uint16_t value) {
    uint8_t buf[3] = {reg, (uint8_t)(value >> 8), (uint8_t)(value & 0xFF)};
    return i2c_write_blocking(I2C_PORT, INA_ADDR, buf, 3, false) == 3;
}

static bool ina228_read_u24(uint8_t reg, uint32_t *out) {
    uint8_t buf[3];
    if (!ina228_read(reg, buf, 3)) return false;
    *out = ((uint32_t)buf[0] << 16) | ((uint32_t)buf[1] << 8) | buf[2];
    return true;
}

static bool ina228_read_s24(uint8_t reg, int32_t *out) {
    uint8_t buf[3];
    if (!ina228_read(reg, buf, 3)) return false;
    int32_t raw = ((int32_t)buf[0] << 16) | ((int32_t)buf[1] << 8) | buf[2];
    if (raw & 0x800000) raw -= 0x1000000;
    *out = raw;
    return true;
}

static bool ina228_read_u40(uint8_t reg, uint64_t *out) {
    uint8_t buf[5];
    if (!ina228_read(reg, buf, 5)) return false;
    uint64_t raw = 0;
    for (int i = 0; i < 5; i++) raw = (raw << 8) | buf[i];
    *out = raw;
    return true;
}

static bool ina228_read_s40(uint8_t reg, int64_t *out) {
    uint8_t buf[5];
    if (!ina228_read(reg, buf, 5)) return false;
    int64_t raw = 0;
    for (int i = 0; i < 5; i++) raw = (raw << 8) | buf[i];
    if (raw & (1LL << 39)) raw -= (1LL << 40);
    *out = raw;
    return true;
}

// ---------------------------------------------------------------------------
// INA228 setup
// ---------------------------------------------------------------------------

static bool ina228_init(void) {
    // Reset device
    if (!ina228_write16(INA_REG_CONFIG, 0x8000)) return false;
    sleep_ms(2);

    // ADC config (ADCCFG 0x01) — each field on its own line
    uint16_t adccfg =
        (0x0B << 12) |   // MODE   [15:12] 0x0B = continuous bus + shunt (no temp)
        (0x02 <<  9) |   // VBUSCT [11:9]  0x02 = 150 us conversion time
        (0x03 <<  6) |   // VSHCT  [8:6]   0x03 = 280 us conversion time
        (0x02 <<  3) |   // VTCT   [5:3]   0x02 = 150 us (unused, but set anyway)
        (0x01 <<  0);    // AVG    [2:0]   0x01 = 4 samples
    if (!ina228_write16(INA_REG_ADCCFG, adccfg)) return false;

    // Write SHUNT_CAL so the chip computes current/power internally
    current_lsb = MAX_I / (1 << 19);
    float cal_val = 13107.2f * 1000000.0f * current_lsb * SHUNT_R * 1.0f;
    if (!ina228_write16(INA_REG_SHUNTCAL, (uint16_t)(cal_val + 0.5f))) return false;
    sleep_ms(1);

    // Reset energy / charge accumulators
    if (!ina228_write16(INA_REG_CONFIG, 0x4000)) return false;
    sleep_ms(1);

    return true;
}

static bool ina228_check_overflow(void) {
    uint8_t buf[2];
    if (!ina228_read(0x0B, buf, 2)) return false;
    uint16_t diag = ((uint16_t)buf[0] << 8) | buf[1];
    if (diag & ((1 << 11) | (1 << 10))) {
        ina228_write16(INA_REG_CONFIG, 0x4000);
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main() {
    stdio_init_all();
    i2c_init(I2C_PORT, 400000);
    gpio_set_function(I2C_SDA, GPIO_FUNC_I2C);
    gpio_set_function(I2C_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(I2C_SDA);
    gpio_pull_up(I2C_SCL);

    // Onboard LED
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
    int led_tick = 0;
    bool ina_ok = false;

    // Try to init INA228 (retry a few times)
    for (int attempt = 0; attempt < 5; attempt++) {
        if (ina228_init()) {
            ina_ok = true;
            break;
        }
        sleep_ms(100);
    }

    while (1) {
        uint32_t t_ms = time_us_32() / 1000;

        if (ina_ok) {
            uint32_t raw_vbus, raw_power;
            int32_t raw_current;
            uint64_t raw_energy;
            int64_t raw_charge;

            if (ina228_read_u24(INA_REG_VBUS, &raw_vbus) &&
                ina228_read_s24(INA_REG_CURRENT, &raw_current) &&
                ina228_read_u24(INA_REG_POWER, &raw_power) &&
                ina228_read_u40(INA_REG_ENERGY, &raw_energy) &&
                ina228_read_s40(INA_REG_CHARGE, &raw_charge)) {

                float voltage_v = (raw_vbus >> 4) * 195.3125e-6f;
                float current_a = (raw_current / 16.0f) * current_lsb;
                float power_w   = raw_power * 3.2f * current_lsb;
                float energy_j  = raw_energy * 16.0f * 3.2f * current_lsb;
                float charge_c  = raw_charge * current_lsb;

                printf("{\"t\":%lu,\"i\":%.6f,\"v\":%.3f,\"p\":%.3f,\"q\":%.6f,\"e\":%.6f}\n",
                       t_ms, current_a, voltage_v, power_w, charge_c, energy_j);

                ina228_check_overflow();
            } else {
                ina_ok = false;
            }

            if (++led_tick >= 50) {
                led_tick = 0;
                gpio_put(PICO_DEFAULT_LED_PIN, !gpio_get(PICO_DEFAULT_LED_PIN));
            }
        } else {
            if ((t_ms % 2000) == 0) {
                printf("{\"t\":%lu,\"error\":\"ina228_not_found\"}\n", t_ms);
                ina_ok = ina228_init();
            }
            if (++led_tick >= 10) {
                led_tick = 0;
                gpio_put(PICO_DEFAULT_LED_PIN, !gpio_get(PICO_DEFAULT_LED_PIN));
            }
        }

        sleep_ms(10);
    }
}
