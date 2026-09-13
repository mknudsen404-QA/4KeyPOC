#include "nvs_flash.h"
#include "iic.h"
#include "xl9555.h"
#include "lvgl_demo.h"


/**
 * @brief       Program entry point(程序入口)
 * @param       None(无)
 * @retval      None(无)
 */
void app_main(void)
{
    esp_err_t ret;
    
    ret = nvs_flash_init();             /* Initialize NVS(初始化NVS) */

    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }

    /* LCD uses GPIO2: do not initialize the vendor LED demo. */
    myiic_init1(); /* Initialize IIC0 (初始化IIC0) */
    xl9555_init();           /* Initialize IO expander chip (IO扩展芯片初始化) */

    lvgl_demo();                        /* Run LVGL demo routine (运行LVGL例程) */
}
  