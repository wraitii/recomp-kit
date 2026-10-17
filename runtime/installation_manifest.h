#pragma once

#include <string>

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
