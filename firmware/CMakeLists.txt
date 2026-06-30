cmake_minimum_required(VERSION 3.13)
set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
include(${PICO_SDK_PATH}/pico_sdk_init.cmake)
project(current_monitor C CXX ASM)
pico_sdk_init()

add_executable(current_monitor main.c)
target_link_libraries(current_monitor
    pico_stdlib
    hardware_i2c
)
pico_enable_stdio_usb(current_monitor 1)
pico_add_extra_outputs(current_monitor)
