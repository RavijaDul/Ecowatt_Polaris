idf_component_register(
    SRCS
        "main.cpp"
        "transport_idf.cpp"
        "modbus.cpp"
        "acquisition.cpp"
    INCLUDE_DIRS "."
    REQUIRES esp_http_client esp_wifi esp_event esp_netif nvs_flash
)
# Use C++17
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
