cmake_minimum_required(VERSION 3.16)

idf_component_register(
  SRCS
    "main.cpp"
    "transport_idf.cpp"
    "modbus.cpp"
    "acquisition.cpp"
    "buffer.cpp"
    "codec.cpp"
    "packetizer.cpp"
    "security.cpp"
    "nvstore.cpp"
    "control.cpp"
    "fota.cpp"
  INCLUDE_DIRS
    "."
  REQUIRES
    app_update         
    esp_http_client
    esp_wifi
    esp_event
    esp_netif          
    nvs_flash
    esp_timer
    freertos
    mbedtls
    esp_pm
)

target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_definitions(${COMPONENT_LIB} PUBLIC LOG_LOCAL_LEVEL=ESP_LOG_INFO)
