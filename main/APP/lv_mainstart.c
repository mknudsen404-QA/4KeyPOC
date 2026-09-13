#include "lv_mainstart.h"
#include "lvgl.h"
#include "xl9555.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char family[12];
    char status[16];
    char activity[48];
    char effort[8];
    uint32_t color;
} agent_slot_t;

typedef struct {
    const char *name;
    const char *event;
    bool hold_to_confirm;
} command_slot_t;

/* Slots are always displayed as "OPERATOR N" — the console is agent-agnostic
 * by design, so no per-agent custom name (from the bridge or elsewhere) is
 * ever shown on screen. */
static agent_slot_t agents[] = {
    {"", "EMPTY", "press to launch", "MED", 0x7A8497},
    {"", "EMPTY", "press to launch", "MED", 0x7A8497},
    {"", "EMPTY", "press to launch", "MED", 0x7A8497},
    {"", "EMPTY", "press to launch", "MED", 0x7A8497},
};

/* Reserved for the future Commands layer screen; not rendered yet. */
static const command_slot_t commands[] __attribute__((unused)) = {
    {"APPROVE", "plan.approve", true},
    {"REVIEW", "review.request", false},
    {"RUN", "command.run", true},
    {"MIC", "voice.hold", false},
    {"/SLASH", "slash.run", true},
    {"BACK", "system.back", false},
};

static const char *efforts[] = {"LOW", "MED", "HIGH", "XHIGH", "MAX"};
static const gpio_num_t EXT_AGENT1_GPIO = GPIO_NUM_44;

static lv_obj_t *agent_header_label;
static lv_obj_t *agent_name_label;
static lv_obj_t *status_dot;
static lv_obj_t *status_label;
static lv_obj_t *activity_dots[4];
static lv_obj_t *effort_label;
static lv_obj_t *agent_cells[4];
static lv_obj_t *agent_dots[4];
static lv_obj_t *agent_key_labels[4];
static lv_obj_t *hint_bar;
static lv_obj_t *hint_label;

#define COLOR_BG        0x0B0E11
#define COLOR_BORDER    0x232830
#define COLOR_DIM       0x7C8994
#define COLOR_DIMMER    0x4E5862
#define COLOR_TEXT      0xF3F5FA
#define COLOR_EFFORT    0xB59AFF
#define COLOR_CELL_BG   0x171C22
#define COLOR_CELL_BRD  0x3A4453
#define COLOR_HINT_IDLE 0x171C22
#define COLOR_HINT_OK   0x52E58A
#define COLOR_HINT_MIC  0xFF4D5A

static uint8_t selected_agent;
static uint8_t effort_index = 2;
static uint8_t candidate_keys;
static uint8_t stable_keys;
static uint8_t candidate_external_keys;
static uint8_t stable_external_keys;
static uint32_t external_changed_at;
static uint32_t changed_at;
static uint32_t feedback_at;
static bool feedback_active;
static bool read_failed;
static bool mic_active;
static char serial_line[256];
static size_t serial_line_len;

static void refresh_screen(void);
static void show_feedback(const char *message);
static void select_agent_slot(uint8_t slot);

static void usb_write_line(const char *line)
{
    if (!usb_serial_jtag_is_driver_installed()) return;
    usb_serial_jtag_write_bytes(line, strlen(line), 0);
}

static lv_obj_t *make_label(lv_align_t align, int x, int y, int width, uint32_t color)
{
    lv_obj_t *obj = lv_label_create(lv_scr_act());
    lv_label_set_text(obj, "");
    if (width > 0) lv_obj_set_width(obj, width);
    lv_obj_set_style_text_color(obj, lv_color_hex(color), 0);
    lv_obj_align(obj, align, x, y);
    return obj;
}

static lv_obj_t *make_dot(lv_obj_t *parent, lv_align_t align, int x, int y, int size)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_set_size(obj, size, size);
    lv_obj_set_style_radius(obj, 2, 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_clear_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(obj, align, x, y);
    return obj;
}

static void cell_click_cb(lv_event_t *e)
{
    uint8_t slot = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    select_agent_slot(slot);
}

static uint32_t status_color(const char *status)
{
    if (strcmp(status, "EMPTY") == 0) return 0x7A8497;
    if (strcmp(status, "LAUNCHED") == 0) return 0xE6EAF2;
    if (strcmp(status, "IDLE") == 0) return 0xE6EAF2;
    if (strcmp(status, "THINKING") == 0) return 0xB59AFF;
    if (strcmp(status, "WORKING") == 0) return 0xFF9F2E;
    if (strcmp(status, "WAITING") == 0) return 0x57D7FF;
    if (strcmp(status, "NEEDS_INPUT") == 0) return 0xFFD84D;
    if (strcmp(status, "BLOCKED") == 0) return 0xFF4D5A;
    if (strcmp(status, "DONE") == 0) return 0x52E58A;
    return 0xE6EAF2;
}

static bool status_is_busy(const char *status)
{
    return strcmp(status, "THINKING") == 0 ||
           strcmp(status, "WORKING") == 0 ||
           strcmp(status, "WAITING") == 0;
}

static void dot_opa_anim_cb(void *var, int32_t value)
{
    lv_obj_set_style_opa((lv_obj_t *)var, (lv_opa_t)value, 0);
}

static void update_activity_dots(uint32_t color, bool busy)
{
    for (uint8_t i = 0; i < 4; i++) {
        lv_obj_set_style_bg_color(activity_dots[i], lv_color_hex(color), 0);
        lv_anim_del(activity_dots[i], dot_opa_anim_cb);
        if (!busy) {
            lv_obj_set_style_opa(activity_dots[i], LV_OPA_40, 0);
            continue;
        }
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, activity_dots[i]);
        lv_anim_set_exec_cb(&a, dot_opa_anim_cb);
        lv_anim_set_values(&a, LV_OPA_30, LV_OPA_COVER);
        lv_anim_set_time(&a, 260);
        lv_anim_set_playback_time(&a, 260);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_set_repeat_delay(&a, 400);
        lv_anim_set_delay(&a, (uint32_t)i * 140);
        lv_anim_start(&a);
    }
}

static void uppercase_copy(char *dst, size_t dst_size, const char *src)
{
    size_t i = 0;
    if (dst_size == 0) return;
    for (; src[i] && i + 1 < dst_size; i++) {
        char c = src[i];
        if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
        if (c == '-') c = '_';
        dst[i] = c;
    }
    dst[i] = '\0';
}

static bool json_int_field(const char *line, const char *field, int *out)
{
    char pattern[32];
    snprintf(pattern, sizeof pattern, "\"%s\":", field);
    const char *start = strstr(line, pattern);
    if (!start) return false;
    start += strlen(pattern);
    *out = atoi(start);
    return true;
}

static bool json_string_field(const char *line, const char *field, char *out, size_t out_size)
{
    char pattern[32];
    snprintf(pattern, sizeof pattern, "\"%s\":\"", field);
    const char *start = strstr(line, pattern);
    if (!start || out_size == 0) return false;
    start += strlen(pattern);
    size_t i = 0;
    while (start[i] && start[i] != '"' && i + 1 < out_size) {
        out[i] = start[i];
        i++;
    }
    out[i] = '\0';
    return true;
}

static void apply_agent_update(const char *line)
{
    if (!strstr(line, "\"event\":\"agent.update\"")) return;

    int slot = 0;
    if (!json_int_field(line, "slot", &slot) || slot < 1 || slot > 4) return;

    agent_slot_t *agent = &agents[slot - 1];
    json_string_field(line, "family", agent->family, sizeof agent->family);
    json_string_field(line, "activity", agent->activity, sizeof agent->activity);
    json_string_field(line, "effort", agent->effort, sizeof agent->effort);

    char status[16];
    if (json_string_field(line, "status", status, sizeof status)) {
        uppercase_copy(agent->status, sizeof agent->status, status);
        agent->color = status_color(agent->status);
    }

    refresh_screen();
    show_feedback("HOST STATUS UPDATED");
    char ack[64];
    snprintf(ack, sizeof ack, "{\"event\":\"agent.update.ack\",\"slot\":%d}\n", slot);
    usb_write_line(ack);
}

static void emit_agent_select(void)
{
    char line[64];
    snprintf(line, sizeof line, "{\"event\":\"agent.select\",\"slot\":%u}\n",
             (unsigned)(selected_agent + 1));
    usb_write_line(line);
}

static void emit_reasoning_apply(void)
{
    char line[96];
    snprintf(line, sizeof line, "{\"event\":\"agent.reasoning.apply\",\"slot\":%u,\"effort\":\"%s\"}\n",
             (unsigned)(selected_agent + 1), efforts[effort_index]);
    usb_write_line(line);
}

static void emit_voice_start(void)
{
    char line[64];
    snprintf(line, sizeof line, "{\"event\":\"voice.hold.start\",\"slot\":%u}\n", (unsigned)(selected_agent + 1));
    usb_write_line(line);
}

static void emit_voice_stop(void)
{
    char line[64];
    snprintf(line, sizeof line, "{\"event\":\"voice.hold.stop\",\"slot\":%u}\n", (unsigned)(selected_agent + 1));
    usb_write_line(line);
}

static void refresh_agent_keys(void)
{
    for (uint8_t i = 0; i < 4; i++) {
        bool is_selected = (i == selected_agent);
        lv_label_set_text_fmt(agent_key_labels[i], "A%u", (unsigned)(i + 1));
        lv_obj_set_style_text_color(agent_key_labels[i],
                                     lv_color_hex(is_selected ? COLOR_TEXT : COLOR_DIM), 0);
        lv_obj_set_style_bg_color(agent_cells[i],
                                   lv_color_hex(is_selected ? COLOR_CELL_BG : COLOR_BG), 0);
        lv_obj_set_style_border_color(agent_cells[i],
                                       lv_color_hex(is_selected ? COLOR_CELL_BRD : COLOR_BG), 0);
        lv_obj_set_style_bg_color(agent_dots[i], lv_color_hex(agents[i].color), 0);
    }
}

static void refresh_screen(void)
{
    const agent_slot_t *agent = &agents[selected_agent];
    lv_label_set_text_fmt(agent_header_label, "OPERATOR %u/4", (unsigned)(selected_agent + 1));
    lv_label_set_text_fmt(effort_label, "EFFORT %s", agent->effort);
    lv_label_set_text_fmt(agent_name_label, "OPERATOR %u", (unsigned)(selected_agent + 1));
    lv_obj_set_style_bg_color(status_dot, lv_color_hex(agent->color), 0);
    lv_label_set_text(status_label, agent->status);
    lv_obj_set_style_text_color(status_label, lv_color_hex(agent->color), 0);
    update_activity_dots(agent->color, status_is_busy(agent->status));
    lv_obj_set_style_bg_color(hint_bar, lv_color_hex(COLOR_HINT_IDLE), 0);
    lv_obj_set_style_text_color(hint_label, lv_color_hex(COLOR_DIM), 0);
    lv_label_set_text(hint_label, "K1 PREV  K2 NEXT  K3 EFFORT  K4 MIC");
    refresh_agent_keys();
}

static void show_feedback(const char *message)
{
    uint32_t bg = mic_active ? COLOR_HINT_MIC : COLOR_HINT_OK;
    lv_obj_set_style_bg_color(hint_bar, lv_color_hex(bg), 0);
    lv_obj_set_style_text_color(hint_label, lv_color_hex(COLOR_BG), 0);
    lv_label_set_text(hint_label, message);
    feedback_active = true;
    feedback_at = lv_tick_get();
}

static void select_previous_agent(void)
{
    selected_agent = (selected_agent + 3) % 4;
    refresh_screen();
    emit_agent_select();
    show_feedback("AGENT SELECTED");
}

static void select_next_agent(void)
{
    selected_agent = (selected_agent + 1) % 4;
    refresh_screen();
    emit_agent_select();
    show_feedback("AGENT SELECTED");
}

static void cycle_effort(void)
{
    effort_index = (effort_index + 1) % 5;
    strncpy(agents[selected_agent].effort, efforts[effort_index], sizeof agents[selected_agent].effort - 1);
    agents[selected_agent].effort[sizeof agents[selected_agent].effort - 1] = '\0';
    refresh_screen();
    emit_reasoning_apply();
    show_feedback("REASONING EFFORT APPLIED");
}

static void handle_key_press(uint8_t rising)
{
    if (rising & 0x01) {
        select_previous_agent();
    }
    if (rising & 0x02) {
        select_next_agent();
    }
    if (rising & 0x04) {
        cycle_effort();
    }
    if (rising & 0x08) {
        mic_active = true;
        emit_voice_start();
        show_feedback("MIC HELD - RELEASE K4 TO STOP");
    }
}

static void handle_key_release(uint8_t falling)
{
    if ((falling & 0x08) && mic_active) {
        mic_active = false;
        emit_voice_stop();
        show_feedback("MIC RELEASED");
    }
}

static void select_agent_slot(uint8_t slot)
{
    if (slot >= 4) return;
    selected_agent = slot;
    refresh_screen();
    emit_agent_select();
    show_feedback("EXTERNAL AGENT KEY");
}

static void poll_external_keys(uint32_t now)
{
    uint8_t pressed = gpio_get_level(EXT_AGENT1_GPIO) == 0 ? 0x01 : 0x00;

    if (pressed != candidate_external_keys) {
        candidate_external_keys = pressed;
        external_changed_at = now;
    }

    if (candidate_external_keys != stable_external_keys && (uint32_t)(now - external_changed_at) >= 40) {
        uint8_t rising = candidate_external_keys & (uint8_t)~stable_external_keys;
        stable_external_keys = candidate_external_keys;
        if (rising & 0x01) {
            select_agent_slot(0);
        }
    }
}

static void poll_host_serial(void)
{
    char buf[64];
    int n;

    while ((n = usb_serial_jtag_read_bytes(buf, sizeof buf, 0)) > 0) {
        for (int i = 0; i < n; i++) {
            char c = buf[i];
            if (c == '\n' || c == '\r') {
                if (serial_line_len > 0) {
                    serial_line[serial_line_len] = '\0';
                    apply_agent_update(serial_line);
                    serial_line_len = 0;
                }
            } else if (serial_line_len + 1 < sizeof serial_line) {
                serial_line[serial_line_len++] = c;
            } else {
                serial_line_len = 0;
            }
        }
    }
}

static void poll_keys(lv_timer_t *timer)
{
    (void)timer;
    uint8_t ports[2];
    uint32_t now = lv_tick_get();

    poll_host_serial();
    poll_external_keys(now);

    if (xl9555_read_byte(ports, sizeof ports) != ESP_OK) {
        lv_label_set_text(hint_label, "KEY READ ERROR");
        read_failed = true;
        candidate_keys = 0;
        stable_keys = 0;
        changed_at = now;
        return;
    }

    if (read_failed) {
        read_failed = false;
        refresh_screen();
    }

    uint8_t pressed = ((uint8_t)~ports[0] >> 4) & 0x0F;
    if (pressed != candidate_keys) {
        candidate_keys = pressed;
        changed_at = now;
    }

    if (candidate_keys != stable_keys && (uint32_t)(now - changed_at) >= 40) {
        uint8_t rising = candidate_keys & (uint8_t)~stable_keys;
        uint8_t falling = stable_keys & (uint8_t)~candidate_keys;
        stable_keys = candidate_keys;
        handle_key_press(rising);
        handle_key_release(falling);
    }

    if (feedback_active && !mic_active && (uint32_t)(now - feedback_at) >= 1100) {
        lv_obj_set_style_bg_color(hint_bar, lv_color_hex(COLOR_HINT_IDLE), 0);
        lv_obj_set_style_text_color(hint_label, lv_color_hex(COLOR_DIM), 0);
        lv_label_set_text(hint_label, "K1 PREV  K2 NEXT  K3 EFFORT  K4 MIC");
        feedback_active = false;
    }
}

void lv_mainstart(void)
{
    if (!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t usb_config = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
        usb_serial_jtag_driver_install(&usb_config);
    }

    gpio_config_t ext_key_config = {
        .pin_bit_mask = 1ULL << EXT_AGENT1_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&ext_key_config);

    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, lv_color_hex(COLOR_BG), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_font(screen, &lv_font_unscii_16, 0);
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);

    /* Top status bar: small chrome text, slot counter left, effort right, divider below. */
    agent_header_label = make_label(LV_ALIGN_TOP_LEFT, 8, 8, 150, COLOR_DIM);
    lv_obj_set_style_text_font(agent_header_label, &lv_font_unscii_8, 0);
    effort_label = make_label(LV_ALIGN_TOP_RIGHT, -8, 8, 150, COLOR_EFFORT);
    lv_obj_set_style_text_font(effort_label, &lv_font_unscii_8, 0);
    lv_obj_set_style_text_align(effort_label, LV_TEXT_ALIGN_RIGHT, 0);

    lv_obj_t *divider = lv_obj_create(screen);
    lv_obj_remove_style_all(divider);
    lv_obj_set_size(divider, 304, 1);
    lv_obj_set_style_bg_color(divider, lv_color_hex(COLOR_BORDER), 0);
    lv_obj_set_style_bg_opa(divider, LV_OPA_COVER, 0);
    lv_obj_clear_flag(divider, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(divider, LV_ALIGN_TOP_LEFT, 8, 22);

    /* Focal agent: big name, status dot + word, activity line. */
    agent_name_label = make_label(LV_ALIGN_TOP_LEFT, 8, 32, 304, COLOR_TEXT);
    lv_obj_set_style_text_font(agent_name_label, &lv_font_montserrat_32, 0);

    status_dot = make_dot(screen, LV_ALIGN_TOP_LEFT, 8, 84, 10);
    status_label = make_label(LV_ALIGN_TOP_LEFT, 24, 81, 260, 0xFF9F2E);

    for (uint8_t i = 0; i < 4; i++) {
        activity_dots[i] = make_dot(screen, LV_ALIGN_TOP_LEFT, 8 + i * 20, 110, 10);
    }

    /* Agent slot strip: four tappable cells, dot + label, selected one highlighted. */
    for (uint8_t i = 0; i < 4; i++) {
        int cell_x = 8 + i * (70 + 8);
        agent_cells[i] = lv_obj_create(screen);
        lv_obj_remove_style_all(agent_cells[i]);
        lv_obj_set_size(agent_cells[i], 70, 30);
        lv_obj_set_style_radius(agent_cells[i], 4, 0);
        lv_obj_set_style_bg_opa(agent_cells[i], LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(agent_cells[i], 1, 0);
        lv_obj_clear_flag(agent_cells[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(agent_cells[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_align(agent_cells[i], LV_ALIGN_TOP_LEFT, cell_x, 148);
        lv_obj_add_event_cb(agent_cells[i], cell_click_cb, LV_EVENT_CLICKED, (void *)(uintptr_t)i);

        agent_dots[i] = make_dot(agent_cells[i], LV_ALIGN_LEFT_MID, 8, 0, 8);

        agent_key_labels[i] = lv_label_create(agent_cells[i]);
        lv_obj_align(agent_key_labels[i], LV_ALIGN_LEFT_MID, 20, 0);
    }

    /* Bottom hint bar: key legend by default, flashes green/red for feedback. */
    hint_bar = lv_obj_create(screen);
    lv_obj_remove_style_all(hint_bar);
    lv_obj_set_size(hint_bar, 304, 34);
    lv_obj_set_style_radius(hint_bar, 3, 0);
    lv_obj_set_style_bg_opa(hint_bar, LV_OPA_COVER, 0);
    lv_obj_clear_flag(hint_bar, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(hint_bar, LV_ALIGN_TOP_LEFT, 8, 188);

    hint_label = lv_label_create(hint_bar);
    lv_obj_set_width(hint_label, 288);
    lv_label_set_long_mode(hint_label, LV_LABEL_LONG_CLIP);
    lv_obj_set_style_text_align(hint_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(hint_label);

    refresh_screen();
    lv_timer_create(poll_keys, 20, NULL);
}
