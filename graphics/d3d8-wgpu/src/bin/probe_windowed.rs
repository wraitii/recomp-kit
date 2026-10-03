//! Main-thread winit lifecycle for the bounded windowed probe.
use d3d8_wgpu::probe::{self, ProbeOptions};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use winit::{
    application::ApplicationHandler,
    event::WindowEvent,
    event_loop::{ActiveEventLoop, ControlFlow, EventLoop},
    window::{Window, WindowId},
};

#[derive(Default)]
struct App {
    window: Option<Arc<Window>>,
    exit_code: i32,
    finished: Option<Instant>,
}
impl ApplicationHandler for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }
        let options = ProbeOptions::default();
        match event_loop.create_window(
            Window::default_attributes()
                .with_title("D3D8 / wgpu unlit triangle probe")
                .with_inner_size(winit::dpi::PhysicalSize::new(options.width, options.height))
                .with_resizable(false),
        ) {
            Ok(window) => {
                let window = Arc::new(window);
                window.request_redraw();
                self.window = Some(window);
            }
            Err(error) => {
                eprintln!("create window: {error}");
                self.exit_code = 2;
                event_loop.exit();
            }
        }
    }
    fn window_event(&mut self, event_loop: &ActiveEventLoop, _: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => {
                if self.finished.is_none() {
                    self.exit_code = 2;
                }
                event_loop.exit();
            }
            WindowEvent::RedrawRequested if self.finished.is_none() => {
                let window = self.window.as_ref().unwrap();
                let size = window.inner_size();
                let options = ProbeOptions {
                    width: size.width,
                    height: size.height,
                    headless: false,
                };
                match probe::run_windowed(&options, Arc::clone(window)) {
                    Ok(report) => {
                        probe::print_report(&report);
                        self.exit_code = if report.all_passed() { 0 } else { 1 };
                    }
                    Err(error) => {
                        eprintln!("probe-windowed unavailable: {error}");
                        self.exit_code = 2;
                    }
                }
                let deadline = Instant::now() + Duration::from_millis(750);
                self.finished = Some(deadline);
                event_loop.set_control_flow(ControlFlow::WaitUntil(deadline));
            }
            _ => {}
        }
    }
    fn about_to_wait(&mut self, event_loop: &ActiveEventLoop) {
        if self
            .finished
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            event_loop.exit();
        }
    }
}
fn main() {
    env_logger::init();
    let event_loop = EventLoop::new().unwrap_or_else(|error| {
        eprintln!("event loop: {error}");
        std::process::exit(2)
    });
    let mut app = App {
        exit_code: 2,
        ..Default::default()
    };
    if let Err(error) = event_loop.run_app(&mut app) {
        eprintln!("event loop: {error}");
        std::process::exit(2);
    }
    std::process::exit(app.exit_code);
}
