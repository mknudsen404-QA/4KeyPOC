#include "lvgl_demo.h"
#include "led.h"
#include "ltdc.h"
#include "touch.h"
#include "esp_timer.h"
#include "lvgl.h"
#include "lv_mainstart.h"


/* Called after SPI DMA finishes reading LVGL's draw buffer. */
static bool codex_flush_done(esp_lcd_panel_io_handle_t io,
                            esp_lcd_panel_io_event_data_t *event, void *context)
{
    (void)io;
    (void)event;
    lv_disp_flush_ready((lv_disp_drv_t *)context);
    return false;
}

/* LV_DEMO_TASK 任务 配置
 * 包括: 任务优先级 堆栈大小 任务句柄 创建任务
 */
#define LV_DEMO_TASK_PRIO   1               /* 任务优先级 */
#define LV_DEMO_STK_SIZE    5 * 1024        /* 任务堆栈大小 */
TaskHandle_t LV_DEMOTask_Handler;           /* 任务句柄 */
void lv_demo_task(void *pvParameters);      /* 任务函数 */

/**
 * @brief       lvgl_demo入口函数
 * @param       无
 * @retval      无
 */
void lvgl_demo(void)
{
    lv_init();              /* 初始化LVGL图形库 */
    lv_port_disp_init();    /* lvgl显示接口初始化,放在lv_init()的后面 */
    lv_port_indev_init();   /* lvgl输入接口初始化,放在lv_init()的后面 */

    /* 为LVGL提供时基单元 */
    const esp_timer_create_args_t lvgl_tick_timer_args = {
        .callback = &increase_lvgl_tick,
        .name = "lvgl_tick"
    };
    esp_timer_handle_t lvgl_tick_timer = NULL;
    ESP_ERROR_CHECK(esp_timer_create(&lvgl_tick_timer_args, &lvgl_tick_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(lvgl_tick_timer, 1 * 1000));

    /* 创建LVGL任务 */
    xTaskCreatePinnedToCore((TaskFunction_t )lv_demo_task,          /* 任务函数 */
                            (const char*    )"lv_demo_task",        /* 任务名称 */
                            (uint16_t       )LV_DEMO_STK_SIZE,      /* 任务堆栈大小 */
                            (void*          )NULL,                  /* 传入给任务函数的参数 */
                            (UBaseType_t    )LV_DEMO_TASK_PRIO,     /* 任务优先级 */
                            (TaskHandle_t*  )&LV_DEMOTask_Handler,  /* 任务句柄 */
                            (BaseType_t     ) 0);                   /* 该任务哪个内核运行 */


}

/**
 * @brief       LVGL运行例程
 * @param       pvParameters : 传入参数(未用到)
 * @retval      无
 */
void lv_demo_task(void *pvParameters)
{
    pvParameters = pvParameters;

    lv_mainstart();     /* 测试的demo */

    while (1)
    {
        lv_timer_handler();             /* LVGL计时器 */
        vTaskDelay(pdMS_TO_TICKS(10));  /* 延时10毫秒 */
    }
}

/**
 * @brief       初始化并注册显示设备
 * @param       无
 * @retval      无
 */
void lv_port_disp_init(void)
{
    void *buf1 = NULL;
    void *buf2 = NULL;

    lcd_init();

    // 先声明在用
    static lv_disp_draw_buf_t disp_buf;   // 保存显示缓冲信息
    static lv_disp_drv_t      disp_drv;   // 显示驱动描述符

    buf1 = heap_caps_malloc(lcddev.width * 60 * sizeof(lv_color_t), MALLOC_CAP_DMA);
    buf2 = heap_caps_malloc(lcddev.width * 60 * sizeof(lv_color_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK((buf1 && buf2) ? ESP_OK : ESP_ERR_NO_MEM);
    lv_disp_draw_buf_init(&disp_buf, buf1, buf2, lcddev.width * 60);

    lv_disp_drv_init(&disp_drv);
    disp_drv.hor_res = lcddev.width;
    disp_drv.ver_res = lcddev.height;
    disp_drv.flush_cb = lvgl_disp_flush_cb;              // 使用前面声明的静态函数
    disp_drv.draw_buf = &disp_buf;
    disp_drv.user_data = lcddev.lcd_panel_handle;

    const esp_lcd_panel_io_callbacks_t callbacks = {
        .on_color_trans_done = codex_flush_done,
    };
    ESP_ERROR_CHECK(esp_lcd_panel_io_register_event_callbacks(
        lcddev.io_handle, &callbacks, &disp_drv));
    lv_disp_drv_register(&disp_drv);
}

/**
 * @brief       初始化并注册输入设备
 * @param       无
 * @retval      无
 */
void lv_port_indev_init(void)
{
    /* 初始化触摸屏 */
    tp_dev.init();

    /* 初始化输入设备 */
    static lv_indev_drv_t indev_drv;
    lv_indev_drv_init(&indev_drv);

    /* 配置输入设备类型 */
    indev_drv.type = LV_INDEV_TYPE_POINTER;

    /* 设置输入设备读取回调函数 */
    indev_drv.read_cb = touchpad_read;

    /* 在LVGL中注册驱动程序，并保存创建的输入设备对象 */
    lv_indev_t *indev_touchpad;
    indev_touchpad = lv_indev_drv_register(&indev_drv);
}

/**
* @brief    将内部缓冲区的内容刷新到显示屏上的特定区域
* @note     可以使用 DMA 或者任何硬件在后台加速执行这个操作
*           但是，需要在刷新完成后调用函数 'lv_disp_flush_ready()'
* @param    disp_drv : 显示设备
* @param    area : 要刷新的区域，包含了填充矩形的对角坐标
* @param    color_map : 颜色数组
* @retval   无
*/
static void lvgl_disp_flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *color_map)
{
    esp_lcd_panel_handle_t panel_handle = (esp_lcd_panel_handle_t)drv->user_data;

    /* 特定区域打点 */
    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel_handle, area->x1, area->y1, area->x2 + 1, area->y2 + 1, color_map));

    /* 重要!!! 通知图形库，已经刷新完毕了 */
    /* DMA completion callback releases this buffer. */
}

/**
 * @brief       告诉LVGL运行时间
 * @param       arg : 传入参数(未用到)
 * @retval      无
 */
static void increase_lvgl_tick(void *arg)
{
    /* 告诉LVGL已经过了多少毫秒 */
    lv_tick_inc(1);
}

/**
 * @brief       获取触摸屏设备的状态
 * @param       无
 * @retval      返回触摸屏设备是否被按下
 */
static bool touchpad_is_pressed(void)
{
    tp_dev.scan();     /* 触摸按键扫描 */

    if (tp_dev.sta & TP_PRES_DOWN)
    {
        return true;
    }

    return false;
}


/**
 * @brief       在触摸屏被按下的时候读取 x、y 坐标
 * @param       x   : x坐标的指针
 * @param       y   : y坐标的指针
 * @retval      无
 */
static void touchpad_get_xy(lv_coord_t *x, lv_coord_t *y)
{
    // 交换 X 和 Y 坐标
    (*x) = tp_dev.y[0];  // 修改 X 为 Y
    (*y) = lcddev.height - tp_dev.x[0];   // 修改 Y 为 X
}

/**
 * @brief       图形库的触摸屏读取回调函数
 * @param       indev_drv   : 触摸屏设备
 * @param       data        : 输入设备数据结构体
 * @retval      无
 */
void touchpad_read(lv_indev_drv_t *indev_drv, lv_indev_data_t *data)
{
    static lv_coord_t last_x = 0;
    static lv_coord_t last_y = 0;

    /* 保存按下的坐标和状态 */
    if(touchpad_is_pressed())
    {
        touchpad_get_xy(&last_x, &last_y);  /* 在触摸屏被按下的时候读取 x、y 坐标 */
        data->state = LV_INDEV_STATE_PR;
    } 
    else
    {
        data->state = LV_INDEV_STATE_REL;
    }

    /* 设置最后按下的坐标 */
    data->point.x = last_x;
    data->point.y = last_y;
}