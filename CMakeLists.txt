cmake_minimum_required(VERSION 3.16)
project(DecentraLabsLocalDCPDemo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)

set(DCP_HEADERS
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/dcplib/include/core
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/dcplib/include/master
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/dcplib/include/slave
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/dcplib/include/ethernet
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/asio/include)

add_executable(dcp_master src/dcp_master.cpp)
add_executable(dcp_slave src/dcp_slave.cpp)
foreach(target dcp_master dcp_slave)
    target_include_directories(${target} PRIVATE ${DCP_HEADERS})
    target_compile_definitions(${target} PRIVATE ASIO_STANDALONE)
    target_link_libraries(${target} PRIVATE Threads::Threads)
    if(WIN32)
        # FMI runtime includes windows.h before the DCP/Asio headers. Keep
        # legacy Winsock and min/max macros out of those translation units.
        target_compile_definitions(${target} PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
        target_link_libraries(${target} PRIVATE ws2_32 wsock32)
    else()
        target_link_libraries(${target} PRIVATE ${CMAKE_DL_LIBS})
    endif()
endforeach()
