#pragma once

#include <string>

namespace installation_manifest_detail {

inline bool safe_relative(const std::string &path) {
    if (path.empty() || path[0] == '/' || path.find('\\') != std::string::npos ||
        path.find(':') != std::string::npos || path.find('\0') != std::string::npos ||
        path.back() == '/')
        return false;
    size_t start = 0;
    while (start < path.size()) {
        const size_t end = path.find('/', start);
        const std::string part = path.substr(start, end == std::string::npos ? end : end - start);
        if (part.empty() || part == "." || part == "..")
            return false;
        if (end == std::string::npos)
            break;
        start = end + 1;
    }
    return true;
}

inline bool path_within(const std::string &root, const std::string &path) {
    return path.size() >= root.size() && path.compare(0, root.size(), root) == 0 &&
           (path.size() == root.size() || path[root.size()] == '/' || path[root.size()] == '\\');
}

} // namespace installation_manifest_detail

struct InstallationManifest {
    bool found = false;
    bool valid = false;
    std::string path;
    std::string game_folder;
    std::string game_id;
    std::string profile_id;
    std::string executable;
    std::string data_root;
    std::string profile_suffix;
    std::string error;
};

InstallationManifest installation_manifest_for_process();
InstallationManifest installation_manifest_location_for_process();
