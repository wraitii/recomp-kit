use crate::RenderError;

#[cfg(target_os = "macos")]
pub(crate) type ReadyEvent = metal::SharedEvent;
#[cfg(not(target_os = "macos"))]
pub(crate) type ReadyEvent = ();

#[cfg(target_os = "macos")]
pub(crate) fn create_ready_event(device: &wgpu::Device) -> Result<ReadyEvent, RenderError> {
    let hal = unsafe { device.as_hal::<wgpu::hal::api::Metal>() }
        .ok_or_else(|| RenderError::new("PresentHandoff", "the device is not a Metal device"))?;
    Ok(hal.raw_device().lock().new_shared_event())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn create_ready_event(_: &wgpu::Device) -> Result<ReadyEvent, RenderError> {
    Err(RenderError::new(
        "PresentHandoff",
        "native GPU synchronization is unavailable on this backend",
    ))
}

#[cfg(target_os = "macos")]
pub(crate) fn signal_ready(
    queue: &wgpu::Queue,
    event: &ReadyEvent,
    value: u64,
) -> Result<*mut u8, RenderError> {
    let hal = unsafe { queue.as_hal::<wgpu::hal::api::Metal>() }
        .ok_or_else(|| RenderError::new("PresentHandoff", "the queue is not a Metal queue"))?;
    metal::objc::rc::autoreleasepool(|| {
        let queue = hal.as_raw().lock();
        let command = queue.new_command_buffer();
        command.encode_signal_event(event, value);
        command.commit();
    });
    let raw: &metal::SharedEventRef = event;
    Ok((raw as *const metal::SharedEventRef).cast_mut().cast())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn signal_ready(
    _: &wgpu::Queue,
    _: &ReadyEvent,
    _: u64,
) -> Result<*mut u8, RenderError> {
    Err(RenderError::new(
        "PresentHandoff",
        "native GPU synchronization is unavailable on this backend",
    ))
}
