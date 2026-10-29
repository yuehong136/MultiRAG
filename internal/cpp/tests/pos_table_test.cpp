#include "../dart_trie.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const std::string &message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void write_fixture(const std::filesystem::path &path, const std::string &contents) {
    std::ofstream file(path);
    file << contents;
    require(file.good(), "cannot write fixture: " + path.string());
}
} // namespace

int main(int argc, char **argv) {
    if (argc != 2) {
        std::cerr << "usage: pos_table_test <owned-fixture-directory>\n";
        return 2;
    }
    try {
        const std::filesystem::path root(argv[1]);
        const auto valid = root / "pos-valid.txt";
        write_fixture(valid, "v\r\n\nn\nn\r\nadj\n");
        POSTable table(valid.string());
        require(table.Load() == 0, "valid POS table did not load");
        require(table.GetPOSIndex("adj") == 0, "POS indices must use dictionary order");
        require(table.GetPOSIndex("n") == 1, "duplicate POS must have one index");
        require(table.GetPOSIndex("v") == 2, "CRLF POS should be normalized");
        require(std::string(table.GetPOS(0)) == "adj", "index 0 must return loaded POS");
        require(std::string(table.GetPOS(1)) == "n", "index 1 must return loaded POS");
        require(std::string(table.GetPOS(2)) == "v", "last index must return loaded POS");
        require(std::string(table.GetPOS(-1)).empty(), "negative POS must be empty");
        require(std::string(table.GetPOS(3)).empty(), "out-of-range POS must be empty");
        require(table.GetPOSIndex("unknown") == -1, "unknown POS must be absent");

        const auto empty = root / "pos-empty.txt";
        write_fixture(empty, "\r\n\n");
        POSTable empty_table(empty.string());
        require(empty_table.Load() == 0, "empty POS table did not load");
        require(std::string(empty_table.GetPOS(0)).empty(), "empty table must have no POS");

        POSTable missing((root / "pos-missing.txt").string());
        require(missing.Load() == -1, "missing POS table must fail");
        require(std::string(missing.GetPOS(0)).empty(), "failed table must have no POS");
        std::cout << "POS table load, lookup, CRLF, duplicates and bounds passed\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
    return 0;
}
