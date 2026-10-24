# OpenCC input regression tests

Run against the real vendored C sources, without the C++ tokenizer or Go runtime:

```sh
cmake -S internal/cpp/tests -B /absolute/owned/opencc-tests -DENABLE_SANITIZERS=ON
cmake --build /absolute/owned/opencc-tests --parallel 2
ctest --test-dir /absolute/owned/opencc-tests --output-on-failure --output-junit /absolute/owned/opencc-tests.xml
```

Use a compiler with AddressSanitizer and UndefinedBehaviorSanitizer support. These
tests cover long UTF-8 entries, multiple values, value-array terminators, truncated
line draining, malformed UTF-8 cleanup, missing files, config paths, and concurrent
reads of independent valid dictionaries/configs. They do not establish thread
safety for shared dictionary handles or the library's global error state.

The dictionary/config buffers hold 4096/8192 bytes respectively. A full buffer
without a newline is treated as truncated, including an exactly full last chunk at
EOF. Dictionary entries are drained and skipped; truncated config lines fail with
`CONFIG_ERROR_PARSE`. Short final lines without a newline remain valid. The boundary
cases lock this conservative policy to prevent partial conversions/configuration.

The standalone test binary does not replace rebuilding `rag_tokenizer_c_api` and
relinking actual CGO consumers. Keep archive, compiler, build flags, real dictionary
and consumer binary hashes together when handing a rebuilt archive to another
checkout. An archive is specific to its OS and architecture.
