fn main() {
    cxx_build::bridge("src/bridge.rs").compile("d3d8-wgpu-cxx");
}
