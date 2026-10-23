use crate::RenderError;

pub(crate) fn register(
    encoder: &mut wgpu::CommandEncoder,
    completed: impl Fn(bool) + Send + Sync + 'static,
) -> Result<bool, RenderError> {
    #[cfg(target_os = "macos")]
    unsafe {
        encoder.as_hal_mut::<wgpu::hal::api::Metal, _, _>(move |hal| {
            let Some(hal) = hal else {
                return Ok(false);
            };
            let command = hal.raw_command_buffer().ok_or_else(|| {
                RenderError::new("Issue", "Metal query command buffer is missing")
            })?;
            let handler = block::ConcreteBlock::new(move |command: &metal::CommandBufferRef| {
                completed(command.status() == metal::MTLCommandBufferStatus::Completed);
            })
            .copy();
            command.add_completed_handler(&handler);
            Ok(true)
        })
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (encoder, completed);
        Ok(false)
    }
}
