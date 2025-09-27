# Minimum CMake version expected by ESP-IDF build system
cmake_minimum_required(VERSION 3.16)

# Register this folder as an ESP-IDF component.
# - SRCS: the compilation units for this component
# - INCLUDE_DIRS: headers are in this same dir ("main/")
# - REQUIRES: other ESP-IDF components we depend on (link + headers)
idf_component_register(
    SRCS
        "main.cpp"
        "transport_idf.cpp"
        "modbus.cpp"
        "acquisition.cpp"
        "buffer.cpp"
        "codec.cpp"
        "packetizer.cpp"   # present in your project; not shown here
    INCLUDE_DIRS "."
    REQUIRES
        esp_http_client    # HTTP client for SIM/cloud
        esp_wifi           # Wi-Fi station mode
        esp_event          # event loop + Wi-Fi/IP events
        esp_netif          # network interface layer
        nvs_flash          # NVS init (Wi-Fi stores creds etc.)
        esp_timer          # high-res monotonic timer (µs)
        mbedtls            # TLS (used by esp_http_client)
)

# Use C++17 features for this component
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)

# Reduce IDF log noise unless you raise it in menuconfig
target_compile_definitions(${COMPONENT_LIB} PUBLIC LOG_LOCAL_LEVEL=ESP_LOG_INFO)
