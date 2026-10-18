#include "../platform/os.h"
#include "../runtime/installation_manifest.h"
#include "controls/json.h"

#include <cstdio>
#include <fstream>
#include <string>

namespace {

std::string parent(const std::string &path) {
    const size_t slash = path.find_last_of('/');
    return slash == std::string::npos ? std::string() : path.substr(0, slash);
}

} // namespace

int main() {
    char executable[4096];
    if (os_exe_path(executable, sizeof executable) != 0) {
        fprintf(stderr, "Configure launcher cannot locate itself\n");
        return 2;
    }
    const std::string game_folder = parent(parent(parent(parent(executable))));
    const std::string manifest_path = game_folder + "/.recomp-toolkit/installation.json";
    OsStat metadata_stat{}, manifest_stat{};
    char canonical_root[4096], canonical_manifest[4096], canonical_self[4096];
    if (os_lstat((game_folder + "/.recomp-toolkit").c_str(), &metadata_stat) != 0 ||
        !metadata_stat.is_dir || metadata_stat.is_symlink ||
        os_lstat(manifest_path.c_str(), &manifest_stat) != 0 || !manifest_stat.is_regular ||
        manifest_stat.is_symlink || manifest_stat.size > 1024 * 1024 ||
        os_realpath(game_folder.c_str(), canonical_root, sizeof canonical_root) != 0 ||
        os_realpath(manifest_path.c_str(), canonical_manifest, sizeof canonical_manifest) != 0 ||
        os_realpath(executable, canonical_self, sizeof canonical_self) != 0 ||
        canonical_root == std::string(canonical_manifest) ||
        !installation_manifest_detail::path_within(canonical_root, canonical_manifest) ||
        canonical_root == std::string(canonical_self) ||
        !installation_manifest_detail::path_within(canonical_root, canonical_self)) {
        fprintf(stderr, "Configure launcher cannot inspect %s\n", manifest_path.c_str());
        return 2;
    }
    std::ifstream input(manifest_path, std::ios::binary);
    if (!input) {
        fprintf(stderr, "Configure launcher cannot read %s\n", manifest_path.c_str());
        return 2;
    }
    std::string text;
    char buffer[4096];
    while (input) {
        input.read(buffer, sizeof buffer);
        const std::streamsize count = input.gcount();
        if (count > 0)
            text.append(buffer, (size_t)count);
        if (text.size() > 1024 * 1024) {
            fprintf(stderr, "Configure launcher manifest exceeded the 1 MiB limit\n");
            return 2;
        }
    }
    controls::Json manifest;
    std::string parse_error;
    const bool parsed = controls::json_parse(text, &manifest, &parse_error);
    const controls::Json *schema = manifest.get("schema");
    if (input.bad() || !parsed || manifest.type != controls::Json::Object || !schema ||
        schema->num("major", 0) != 1) {
        fprintf(stderr, "Configure launcher found an invalid installation manifest: %s\n",
                parse_error.c_str());
        return 2;
    }
    const controls::Json *launch_paths = manifest.get("launchPaths");
    const std::string role = launch_paths ? launch_paths->str("game", "") : "";
    const std::string configure_role = launch_paths ? launch_paths->str("configure", "") : "";
    if (!installation_manifest_detail::safe_relative(role) ||
        !installation_manifest_detail::safe_relative(configure_role)) {
        fprintf(stderr, "Configure launcher manifest has invalid launch roles\n");
        return 2;
    }
    char canonical_main[4096], canonical_configure[4096];
    const std::string main_path = game_folder + "/" + role;
    const std::string configure_path = game_folder + "/" + configure_role;
    if (os_realpath(main_path.c_str(), canonical_main, sizeof canonical_main) != 0 ||
        os_realpath(configure_path.c_str(), canonical_configure, sizeof canonical_configure) != 0 ||
        canonical_root == std::string(canonical_main) ||
        !installation_manifest_detail::path_within(canonical_root, canonical_main) ||
        canonical_root == std::string(canonical_configure) ||
        !installation_manifest_detail::path_within(canonical_root, canonical_configure) ||
        std::string(canonical_configure) != canonical_self) {
        fprintf(stderr, "Configure launcher role does not match its manifest\n");
        return 2;
    }
    OsStat main_stat{};
    if (os_stat(canonical_main, &main_stat) != 0 || !main_stat.is_regular) {
        fprintf(stderr, "Configure launcher target is missing: %s\n", canonical_main);
        return 2;
    }
    const char *arguments[] = {canonical_main, "--configure", nullptr};
    int64_t pid = 0;
    if (os_spawn(arguments, &pid) != 0) {
        fprintf(stderr, "Configure launcher cannot start %s\n", canonical_main);
        return 2;
    }
    int status = 0;
    return os_wait(pid, &status) == 0 ? status : 2;
}
