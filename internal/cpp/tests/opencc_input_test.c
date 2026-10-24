#include "../opencc/config_reader.h"
#include "../opencc/dictionary/text.h"
#include "../opencc/encoding.h"

#include <fcntl.h>
#include <pthread.h>
#include <unistd.h>

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #condition); \
        exit(1); \
    } \
} while (0)

/* Exercise the value allocation boundary independently of the bounded file reader. */
extern int parse_entry(const char *buff, entry *entry_i);

static const char *scratch_dir;
static unsigned fixture_id;

static void write_fixture(char *path, size_t path_size, const char *data, size_t length) {
    CHECK(snprintf(path, path_size, "%s/opencc-%ld-%u.txt", scratch_dir, (long)getpid(), fixture_id++) < (int)path_size);
    FILE *fp = fopen(path, "wb");
    CHECK(fp != NULL);
    CHECK(fwrite(data, 1, length, fp) == length);
    CHECK(fclose(fp) == 0);
}

static dictionary_t open_text(const char *data) {
    char path[4096];
    write_fixture(path, sizeof(path), data, strlen(data));
    dictionary_t dictionary = dictionary_text_open(path);
    CHECK(unlink(path) == 0);
    return dictionary;
}

static const ucs4_t *const *match(dictionary_t dictionary, const char *key) {
    CHECK(dictionary != (dictionary_t)-1);
    ucs4_t *word = utf8_to_ucs4(key, 0);
    CHECK(word != (ucs4_t *)-1);
    size_t length = 0;
    const ucs4_t *const *values = dictionary_text_match_longest(dictionary, word, 0, &length);
    if (values != NULL)
        CHECK(length == ucs4len(word));
    free(word);
    return values;
}

static void check_value(const ucs4_t *value, const char *expected) {
    CHECK(value != NULL);
    char *actual = ucs4_to_utf8(value, 0);
    CHECK(actual != (char *)-1);
    CHECK(strcmp(actual, expected) == 0);
    free(actual);
}

static char *sized_line(const char *prefix, size_t length, int newline) {
    size_t prefix_length = strlen(prefix);
    CHECK(length > prefix_length + (size_t)newline);
    char *line = malloc(length + 1);
    CHECK(line != NULL);
    memcpy(line, prefix, prefix_length);
    memset(line + prefix_length, 'a', length - prefix_length);
    if (newline)
        line[length - 1] = '\n';
    line[length] = '\0';
    return line;
}

static void long_utf8(void) {
    char value[601], line[1024];
    memset(value, 'a', sizeof(value) - 1);
    value[sizeof(value) - 1] = '\0';
    snprintf(line, sizeof(line), "繁體\t%s 简体\n", value);
    dictionary_t dictionary = open_text(line);
    const ucs4_t *const *values = match(dictionary, "繁體");
    CHECK(values != NULL);
    check_value(values[0], value);
    check_value(values[1], "简体");
    CHECK(values[2] == NULL);
    dictionary_text_close(dictionary);
}

static void no_final_newline(void) {
    dictionary_t dictionary = open_text("後\t后");
    const ucs4_t *const *values = match(dictionary, "後");
    CHECK(values != NULL);
    check_value(values[0], "后");
    CHECK(values[1] == NULL);
    dictionary_text_close(dictionary);
}

static void dictionary_boundary(void) {
    for (size_t length = 4094; length <= 4096; length++) {
        for (int newline = 0; newline <= 1; newline++) {
            char *line = sized_line("key\t", length, newline);
            dictionary_t dictionary = open_text(line);
            const ucs4_t *const *values = match(dictionary, "key");
            int complete = length < 4095 || (length == 4095 && newline);
            CHECK((values != NULL) == complete);
            if (complete) {
                line[length - (size_t)newline] = '\0';
                check_value(values[0], line + 4);
                CHECK(values[1] == NULL);
            }
            dictionary_text_close(dictionary);
            free(line);
        }
    }
}

static void truncated_tail(void) {
    char *line = sized_line("discard\t", 9000, 0);
    const char *tail = "tail\twrong\nkeep\tcorrect\n";
    line = realloc(line, 9000 + strlen(tail) + 1);
    CHECK(line != NULL);
    strcpy(line + 9000, tail);
    dictionary_t dictionary = open_text(line);
    CHECK(match(dictionary, "discard") == NULL);
    CHECK(match(dictionary, "tail") == NULL);
    const ucs4_t *const *values = match(dictionary, "keep");
    CHECK(values != NULL);
    check_value(values[0], "correct");
    dictionary_text_close(dictionary);
    free(line);
}

static void value_counts(void) {
    /* Check exact capacities first so a file-reader failure cannot mask the overflow. */
    const size_t counts[] = {1024, 2048, 1023, 1025};
    for (size_t n = 0; n < sizeof(counts) / sizeof(counts[0]); n++) {
        size_t count = counts[n];
        char *line = malloc(4 + count * 2 + 1);
        CHECK(line != NULL);
        memcpy(line, "key\t", 4);
        for (size_t i = 0; i < count; i++) {
            line[4 + i * 2] = 'v';
            line[5 + i * 2] = (i + 1 == count) ? '\n' : ' ';
        }
        line[4 + count * 2] = '\0';
        entry parsed = {0};
        CHECK(parse_entry(line, &parsed) == 0);
        for (size_t i = 0; i < count; i++) {
            check_value(parsed.value[i], "v");
            free(parsed.value[i]);
        }
        CHECK(parsed.value[count] == NULL);
        free(parsed.value);
        free(parsed.key);
        if (count <= 1025) {
            dictionary_t dictionary = open_text(line);
            const ucs4_t *const *values = match(dictionary, "key");
            CHECK(values != NULL);
            for (size_t i = 0; i < count; i++)
                check_value(values[i], "v");
            CHECK(values[count] == NULL);
            dictionary_text_close(dictionary);
        }
        free(line);
    }
}

static int descriptor_count(void) {
    int count = 0;
    for (int fd = 0; fd < 1024; fd++)
        if (fcntl(fd, F_GETFD) != -1)
            count++;
    return count;
}

static void invalid_utf8(void) {
    const char *bad[] = {"\xff\tv\n", "key\t\xff\n", "key\tv \xff\n", "key\tv \xe4\n"};
    int before = descriptor_count();
    for (size_t n = 0; n < sizeof(bad) / sizeof(bad[0]); n++) {
        for (int repeat = 0; repeat < 32; repeat++)
            CHECK(open_text(bad[n]) == (dictionary_t)-1);
        if (n > 0) {
            entry parsed = {0};
            CHECK(parse_entry(bad[n], &parsed) == -1);
            CHECK(parsed.key == NULL);
            CHECK(parsed.value == NULL);
        }
    }
    CHECK(descriptor_count() == before);
}

static void missing_dictionary(void) {
    char path[4096];
    write_fixture(path, sizeof(path), "key\tv\n", 6);
    CHECK(unlink(path) == 0);
    CHECK(dictionary_text_open(path) == (dictionary_t)-1);
}

static config_t open_config(const char *data) {
    char path[4096];
    write_fixture(path, sizeof(path), data, strlen(data));
    config_t config = config_open(path, scratch_dir);
    CHECK(unlink(path) == 0);
    return config;
}

static void config_boundary(void) {
    for (size_t length = 8190; length <= 8192; length++) {
        for (int newline = 0; newline <= 1; newline++) {
            char *line = sized_line("title=", length, newline);
            config_t config = open_config(line);
            int complete = length < 8191 || (length == 8191 && newline);
            CHECK((config != (config_t)-1) == complete);
            if (complete)
                config_close(config);
            else
                CHECK(config_errno() == CONFIG_ERROR_PARSE);
            free(line);
        }
    }
}

static void config_truncation(void) {
    const char *prefixes[] = {"title=", "# comment ", "description="};
    int before = descriptor_count();
    for (size_t i = 0; i < sizeof(prefixes) / sizeof(prefixes[0]); i++) {
        char *line = sized_line(prefixes[i], 8191, 0);
        const char *tail = "title=tail\n";
        line = realloc(line, 8191 + strlen(tail) + 1);
        CHECK(line != NULL);
        strcpy(line + 8191, tail);
        CHECK(open_config(line) == (config_t)-1);
        CHECK(config_errno() == CONFIG_ERROR_PARSE);
        free(line);
    }
    config_t config = open_config("title=有效\ndescription=complete");
    CHECK(config != (config_t)-1);
    config_close(config);
    CHECK(descriptor_count() == before);
}

static void config_home_path(void) {
    char path[4096];
    const char *data = "title=有效\ndescription=complete";
    write_fixture(path, sizeof(path), data, strlen(data));
    const char *basename = strrchr(path, '/');
    CHECK(basename != NULL);
    config_t config = config_open(basename + 1, scratch_dir);
    CHECK(config != (config_t)-1);
    config_close(config);
    CHECK(unlink(path) == 0);
}

typedef struct {
    char path[4096];
    char key[32];
    int config;
} thread_input;

static void *read_independent(void *argument) {
    thread_input *input = argument;
    for (int i = 0; i < 200; i++) {
        if (input->config) {
            config_t config = config_open(input->path, scratch_dir);
            CHECK(config != (config_t)-1);
            config_close(config);
        } else {
            dictionary_t dictionary = dictionary_text_open(input->path);
            const ucs4_t *const *values = match(dictionary, input->key);
            CHECK(values != NULL);
            check_value(values[0], input->key);
            dictionary_text_close(dictionary);
        }
    }
    return NULL;
}

static void parallel_read(int config) {
    pthread_t threads[8];
    thread_input inputs[8];
    for (size_t i = 0; i < 8; i++) {
        inputs[i].config = config;
        snprintf(inputs[i].key, sizeof(inputs[i].key), "key%zu", i);
        char data[4096] = {0};
        if (config) {
            for (size_t n = 0; n < 30; n++)
                strcat(data, n % 2 == 0 ? "title=value\n" : "description=value\n");
        } else {
            for (size_t n = 0; n < 30; n++) {
                char line[128];
                snprintf(line, sizeof(line), "%s\t%s\n", inputs[i].key, inputs[i].key);
                strcat(data, line);
            }
        }
        write_fixture(inputs[i].path, sizeof(inputs[i].path), data, strlen(data));
    }
    for (size_t i = 0; i < 8; i++)
        CHECK(pthread_create(&threads[i], NULL, read_independent, &inputs[i]) == 0);
    for (size_t i = 0; i < 8; i++) {
        CHECK(pthread_join(threads[i], NULL) == 0);
        CHECK(unlink(inputs[i].path) == 0);
    }
}

static void string_copy(void) {
    const size_t lengths[] = {0, 1, 8192};
    for (size_t i = 0; i < sizeof(lengths) / sizeof(lengths[0]); i++) {
        size_t length = lengths[i];
        char *original = malloc(length + 1);
        CHECK(original != NULL);
        memset(original, 'a', length);
        original[length] = '\0';
        char *copy = mstrcpy(original);
        CHECK(copy != original);
        CHECK(strcmp(copy, original) == 0);
        CHECK(copy[length] == '\0');
        free(copy);
        free(original);
    }
}

int main(int argc, char **argv) {
    CHECK(argc == 3);
    scratch_dir = argv[2];
    const char *test = argv[1];
    if (strcmp(test, "long_utf8") == 0) long_utf8();
    else if (strcmp(test, "no_final_newline") == 0) no_final_newline();
    else if (strcmp(test, "dictionary_boundary") == 0) dictionary_boundary();
    else if (strcmp(test, "truncated_tail") == 0) truncated_tail();
    else if (strcmp(test, "value_counts") == 0) value_counts();
    else if (strcmp(test, "invalid_utf8") == 0) invalid_utf8();
    else if (strcmp(test, "missing_dictionary") == 0) missing_dictionary();
    else if (strcmp(test, "config_boundary") == 0) config_boundary();
    else if (strcmp(test, "config_truncation") == 0) config_truncation();
    else if (strcmp(test, "config_home_path") == 0) config_home_path();
    else if (strcmp(test, "parallel_dictionary") == 0) parallel_read(0);
    else if (strcmp(test, "parallel_config") == 0) parallel_read(1);
    else if (strcmp(test, "string_copy") == 0) string_copy();
    else CHECK(0);
    return 0;
}
