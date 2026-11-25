cmake_minimum_required(VERSION 3.21)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if ("${CMAKE_SYSTEM_NAME}" STREQUAL "iOS" OR IOS)
    set(CMAKE_OSX_ARCHITECTURES "arm64")
else()
    set(CMAKE_OSX_ARCHITECTURES "arm64;x86_64")
endif()
set(CMAKE_CXX_VISIBILITY_PRESET hidden)

project(gdquiet VERSION 2.0.0)

set(_default_supabase_url "")
if (DEFINED ENV{GDQUIET_SUPABASE_URL} AND NOT "$ENV{GDQUIET_SUPABASE_URL}" STREQUAL "")
    set(_default_supabase_url "$ENV{GDQUIET_SUPABASE_URL}")
elseif (DEFINED ENV{SUPABASE_URL} AND NOT "$ENV{SUPABASE_URL}" STREQUAL "")
    set(_default_supabase_url "$ENV{SUPABASE_URL}")
endif()

set(_default_supabase_anon_key "")
if (DEFINED ENV{GDQUIET_SUPABASE_ANON_KEY} AND NOT "$ENV{GDQUIET_SUPABASE_ANON_KEY}" STREQUAL "")
    set(_default_supabase_anon_key "$ENV{GDQUIET_SUPABASE_ANON_KEY}")
elseif (DEFINED ENV{SUPABASE_ANON_KEY} AND NOT "$ENV{SUPABASE_ANON_KEY}" STREQUAL "")
    set(_default_supabase_anon_key "$ENV{SUPABASE_ANON_KEY}")
elseif (DEFINED ENV{SUPABASE_PUBLISHABLE_KEY} AND NOT "$ENV{SUPABASE_PUBLISHABLE_KEY}" STREQUAL "")
    set(_default_supabase_anon_key "$ENV{SUPABASE_PUBLISHABLE_KEY}")
endif()

set(GDQUIET_SUPABASE_URL "${_default_supabase_url}" CACHE STRING "Supabase project URL baked into the mod")
set(GDQUIET_SUPABASE_ANON_KEY "${_default_supabase_anon_key}" CACHE STRING "Supabase anon/publishable key baked into the mod")

string(REPLACE "\\" "\\\\" GDQUIET_SUPABASE_URL_ESCAPED "${GDQUIET_SUPABASE_URL}")
string(REPLACE "\"" "\\\"" GDQUIET_SUPABASE_URL_ESCAPED "${GDQUIET_SUPABASE_URL_ESCAPED}")
string(REPLACE "\\" "\\\\" GDQUIET_SUPABASE_ANON_KEY_ESCAPED "${GDQUIET_SUPABASE_ANON_KEY}")
string(REPLACE "\"" "\\\"" GDQUIET_SUPABASE_ANON_KEY_ESCAPED "${GDQUIET_SUPABASE_ANON_KEY_ESCAPED}")

configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/gdquiet_config.hpp.in
    ${CMAKE_CURRENT_BINARY_DIR}/generated/gdquiet_config.hpp
    @ONLY
)

# Add all source files inside src (recursively)
file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS src/*.cpp)

# Set up the mod binary
add_library(${PROJECT_NAME} SHARED ${SOURCES})
target_include_directories(${PROJECT_NAME} PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)

if (NOT DEFINED ENV{GEODE_SDK})
    message(FATAL_ERROR "Unable to find Geode SDK! Please define GEODE_SDK environment variable to point to Geode")
else()
    message(STATUS "Found Geode: $ENV{GEODE_SDK}")
endif()

add_subdirectory($ENV{GEODE_SDK} ${CMAKE_CURRENT_BINARY_DIR}/geode)

# Set up dependencies, resources, and link Geode.
setup_geode_mod(${PROJECT_NAME})
