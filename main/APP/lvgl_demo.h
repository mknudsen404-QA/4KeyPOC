#ifndef __LVGL_DEMO_H
#define __LVGL_DEMO_H

#include "lvgl.h"

/* 函数声明 */
void lvgl_demo(void);                                                                               /* lvgl_demo入口函数 */
void lv_port_disp_init(void);                                                                       /* 初始化并注册显示设备 */
void lv_port_indev_init(void);                                                                      /* 初始化并注册输入设备 */
void touchpad_read(lv_indev_drv_t *indev_drv, lv_indev_data_t *data);                               /* 图形库的触摸屏读取回调函数 */
static void increase_lvgl_tick(void *arg);                                                          /* 告诉LVGL运行时间 */
static void lvgl_disp_flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *color_map);   /* 将内部缓冲区的内容刷新到显示屏上的特定区域 */

#endif
