use super::gpu::D3d9Gpu;
use crate::RenderError;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

pub(crate) const D3DQUERYTYPE_EVENT: u32 = 8;
pub(crate) const D3DQUERYTYPE_OCCLUSION: u32 = 9;

pub(crate) enum QueryPoll {
    Pending,
    Ready(u32),
}

struct QueryRecord {
    kind: u32,
    generation: u64,
    done: Arc<AtomicU64>,
}

pub(crate) struct D3d9Queries {
    next_handle: u32,
    next_generation: u64,
    records: BTreeMap<u32, QueryRecord>,
}

impl D3d9Queries {
    pub fn new() -> Self {
        Self {
            next_handle: 1,
            next_generation: 1,
            records: BTreeMap::new(),
        }
    }

    pub fn create(&mut self, kind: u32) -> Result<u32, RenderError> {
        match kind {
            D3DQUERYTYPE_EVENT => {}
            D3DQUERYTYPE_OCCLUSION => {
                return Err(RenderError::new(
                    "CreateQuery",
                    "D3DQUERYTYPE_OCCLUSION needs a pixel count but wgpu 27 occlusion queries are boolean on Metal",
                ));
            }
            other => {
                return Err(RenderError::new(
                    "CreateQuery",
                    format!("D3DQUERYTYPE {other} is not implemented"),
                ));
            }
        }
        let handle = self.next_handle;
        self.next_handle = self.next_handle.wrapping_add(1).max(1);
        self.records.insert(
            handle,
            QueryRecord {
                kind,
                generation: 0,
                done: Arc::new(AtomicU64::new(0)),
            },
        );
        Ok(handle)
    }

    pub fn destroy(&mut self, handle: u32) -> Result<(), RenderError> {
        if self.records.remove(&handle).is_none() {
            return Err(RenderError::invalid("DestroyQuery", "unknown query handle"));
        }
        Ok(())
    }

    pub fn query_type(&self, handle: u32) -> Result<u32, RenderError> {
        self.records
            .get(&handle)
            .map(|record| record.kind)
            .ok_or_else(|| RenderError::invalid("GetType", "unknown query handle"))
    }

    pub fn data_size(kind: u32) -> Result<u32, RenderError> {
        match kind {
            D3DQUERYTYPE_EVENT | D3DQUERYTYPE_OCCLUSION => Ok(4),
            other => Err(RenderError::new(
                "GetDataSize",
                format!("D3DQUERYTYPE {other} is not implemented"),
            )),
        }
    }

    pub fn issue(&mut self, gpu: &D3d9Gpu, handle: u32, begin: bool) -> Result<(), RenderError> {
        if gpu.is_blocked() {
            return Err(RenderError::new("Issue", "the device is not operational"));
        }
        let record = self
            .records
            .get_mut(&handle)
            .ok_or_else(|| RenderError::invalid("Issue", "unknown query handle"))?;
        if record.kind != D3DQUERYTYPE_EVENT {
            return Err(RenderError::new("Issue", "query type is not implemented"));
        }
        if begin {
            return Err(RenderError::invalid(
                "Issue",
                "D3DISSUE_BEGIN is invalid for event queries",
            ));
        }
        let generation = self.next_generation;
        self.next_generation = self.next_generation.wrapping_add(1).max(1);
        let done = Arc::clone(&record.done);
        record.generation = generation;
        gpu.queue().submit(std::iter::empty());
        gpu.queue().on_submitted_work_done(move || {
            done.fetch_max(generation, Ordering::AcqRel);
        });
        Ok(())
    }

    pub fn get_data(
        &mut self,
        gpu: &D3d9Gpu,
        handle: u32,
        flush: bool,
    ) -> Result<QueryPoll, RenderError> {
        if gpu.is_blocked() {
            return Err(RenderError::new("GetData", "the device is not operational"));
        }
        let (kind, generation, done) = {
            let record = self
                .records
                .get(&handle)
                .ok_or_else(|| RenderError::invalid("GetData", "unknown query handle"))?;
            (record.kind, record.generation, Arc::clone(&record.done))
        };
        if kind != D3DQUERYTYPE_EVENT {
            return Err(RenderError::new("GetData", "query type is not implemented"));
        }
        if flush {
            gpu.queue().submit(std::iter::empty());
        }
        if let Err(error) = gpu.device.poll(wgpu::PollType::Poll) {
            return Err(RenderError::new("GetData", error.to_string()));
        }
        if generation == 0 || done.load(Ordering::Acquire) >= generation {
            Ok(QueryPoll::Ready(1))
        } else {
            Ok(QueryPoll::Pending)
        }
    }

    pub fn reset(&mut self, _gpu: &D3d9Gpu) {
        for record in self.records.values_mut() {
            record.generation = 0;
        }
    }
}
