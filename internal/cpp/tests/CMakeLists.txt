cmake_minimum_required(VERSION 3.20)
project(opencc_input_tests C)

enable_testing()
find_package(Threads REQUIRED)
option(ENABLE_SANITIZERS "Check OpenCC input handling with ASan and UBSan" OFF)

# Keep the test main outside opencc/, which is globbed into the production archive.
file(GLOB opencc_sources CONFIGURE_DEPENDS ../opencc/*.c ../opencc/dictionary/*.c)
add_executable(opencc_input_test opencc_input_test.c ${opencc_sources})
target_compile_features(opencc_input_test PRIVATE c_std_99)
target_link_libraries(opencc_input_test PRIVATE Threads::Threads)
target_compile_options(opencc_input_test PRIVATE -Wall -Wextra)
if(ENABLE_SANITIZERS)
    target_compile_options(opencc_input_test PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer -g)
    target_link_options(opencc_input_test PRIVATE -fsanitize=address,undefined)
endif()

foreach(case IN ITEMS long_utf8 no_final_newline dictionary_boundary truncated_tail
        value_counts invalid_utf8 missing_dictionary config_boundary config_truncation
        config_home_path parallel_dictionary parallel_config string_copy)
    add_test(NAME opencc_${case} COMMAND opencc_input_test ${case} ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
