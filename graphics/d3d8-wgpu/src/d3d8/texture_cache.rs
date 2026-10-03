//! Identity-keyed GPU texture cache for the D3D8 draw path.
//!
//! The guest bridge uploads a whole texture level on a bind unless the CPU
//! bytes are known unchanged. Comparing every level's bytes per draw is
//! cheaper than converting and uploading, but the guest already tells us when
//! a level can change: [`TextureKey`] names the guest texture and mip level,
//! and `generation` is bumped by the kit on every write path
//! (`LockRect`/`UnlockRect`, `UpdateTexture`). A bind whose key is present
//! with the same generation, dimensions and format is a hit and reuses the
//! resident wgpu texture verbatim. A lock still open at draw time is passed
//! as `dirty`, which forces a miss so bytes being written through the staged
//! pointer are never assumed unchanged.
//!
//! Keying by identity (not by texture stage) is what makes the same texture
//! bound at stage 0 then stage 1, or re-bound across draws, cost one upload
//! total.

use std::collections::HashMap;

/// Identity of one GPU-resident level. `texture_id` is the kit's COM object
/// id, which is unique for the process lifetime (ids are never reused);
/// `level` is the mip level the bytes came from, so a future non-zero-level
/// upload cannot alias level 0.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub struct TextureKey {
    pub texture_id: u32,
    pub level: u32,
}

impl TextureKey {
    pub const fn new(texture_id: u32, level: u32) -> Self {
        Self { texture_id, level }
    }
}

/// Bookkeeping for one cached resource. The payload is generic so the cache
/// logic (identity, generation, dimensions, eviction) is unit-testable
/// without a GPU device; the device uses it with [`crate::d3d8::device`]'s
/// `CachedTexture`.
struct CacheEntry<T> {
    resource: T,
    generation: u64,
    width: u32,
    height: u32,
    format: u32,
    last_seen: u64,
}

/// A small LRU cache of uploaded textures, keyed by [`TextureKey`].
///
/// Entries are normally removed by the kit's texture release; `max_entries`
/// is a safety net against an unbounded cache in a long session. Eviction just
/// drops the GPU resource, so it can never make a later bind wrong, only
/// re-upload.
pub struct TextureCache<T> {
    entries: HashMap<TextureKey, CacheEntry<T>>,
    clock: u64,
    max_entries: usize,
    /// Cache decisions, for unit tests and the texture-statistics report.
    pub hits: u64,
    pub misses: u64,
}

impl<T> TextureCache<T> {
    pub fn new(max_entries: usize) -> Self {
        Self {
            entries: HashMap::new(),
            clock: 0,
            max_entries,
            hits: 0,
            misses: 0,
        }
    }

    /// Record a bind and report whether the resident resource can be reused.
    ///
    /// A hit requires the same key, generation and dimensions/format, and
    /// `force` (an open lock) is always a miss. Every call bumps the LRU
    /// clock; a hit refreshes `last_seen`.
    pub fn lookup(
        &mut self,
        key: TextureKey,
        generation: u64,
        force: bool,
        width: u32,
        height: u32,
        format: u32,
    ) -> bool {
        self.clock += 1;
        let clock = self.clock;
        let hit = matches!(
            self.entries.get_mut(&key),
            Some(entry)
                if !force
                    && entry.generation == generation
                    && entry.width == width
                    && entry.height == height
                    && entry.format == format
        );
        if hit {
            if let Some(entry) = self.entries.get_mut(&key) {
                entry.last_seen = clock;
            }
            self.hits += 1;
        } else {
            self.misses += 1;
        }
        hit
    }

    /// Record that `key`'s resident resource was re-uploaded with a new
    /// generation (an in-place `write_texture`). Metadata and LRU time update;
    /// the resource itself is untouched.
    pub fn mark_uploaded(
        &mut self,
        key: TextureKey,
        generation: u64,
        width: u32,
        height: u32,
        format: u32,
    ) {
        self.clock += 1;
        let clock = self.clock;
        if let Some(entry) = self.entries.get_mut(&key) {
            entry.generation = generation;
            entry.width = width;
            entry.height = height;
            entry.format = format;
            entry.last_seen = clock;
        }
    }

    pub fn resource(&self, key: TextureKey) -> Option<&T> {
        self.entries.get(&key).map(|entry| &entry.resource)
    }

    pub fn resource_mut(&mut self, key: TextureKey) -> Option<&mut T> {
        self.entries.get_mut(&key).map(|entry| &mut entry.resource)
    }

    /// Store a (possibly replacement) resource for `key`, evicting the least
    /// recently seen entry first when the cache is at capacity.
    pub fn insert(
        &mut self,
        key: TextureKey,
        resource: T,
        generation: u64,
        width: u32,
        height: u32,
        format: u32,
    ) {
        self.clock += 1;
        let clock = self.clock;
        if !self.entries.contains_key(&key) && self.entries.len() >= self.max_entries {
            self.evict_one();
        }
        self.entries.insert(
            key,
            CacheEntry {
                resource,
                generation,
                width,
                height,
                format,
                last_seen: clock,
            },
        );
    }

    /// Remove one key, returning its resource so the caller can drop it
    /// deterministically.
    pub fn remove(&mut self, key: TextureKey) -> Option<T> {
        self.entries.remove(&key).map(|entry| entry.resource)
    }

    /// Drop every level of a guest texture when it is destroyed. Returns the
    /// number of entries removed. The identity is never reused, so no stale
    /// resource can survive to a new texture with the same id.
    pub fn remove_texture(&mut self, texture_id: u32) -> usize {
        let before = self.entries.len();
        self.entries.retain(|key, _| key.texture_id != texture_id);
        before - self.entries.len()
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn evict_one(&mut self) {
        let victim = self
            .entries
            .iter()
            .min_by_key(|(_, entry)| entry.last_seen)
            .map(|(key, _)| *key);
        if let Some(key) = victim {
            self.entries.remove(&key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(id: u32) -> TextureKey {
        TextureKey::new(id, 0)
    }

    #[test]
    fn same_identity_and_generation_hits() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(key(7), 10, 3, 64, 32, 21);
        // Binding the same texture again (at another stage or the next draw)
        // must not upload.
        assert!(cache.lookup(key(7), 3, false, 64, 32, 21));
        assert_eq!(cache.hits, 1);
        assert_eq!(cache.misses, 0);
    }

    #[test]
    fn generation_bump_misses() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(key(7), 10, 3, 64, 32, 21);
        assert!(!cache.lookup(key(7), 4, false, 64, 32, 21));
        assert_eq!(cache.misses, 1);
        // The old generation still matches the still-resident resource until
        // the caller replaces it; the cache key does not churn on a miss.
        assert!(cache.lookup(key(7), 3, false, 64, 32, 21));
    }

    #[test]
    fn dimensions_and_format_must_match() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(key(7), 10, 1, 64, 32, 21);
        assert!(!cache.lookup(key(7), 1, false, 32, 32, 21));
        assert!(!cache.lookup(key(7), 1, false, 64, 32, 22));
    }

    #[test]
    fn identity_key_separates_textures_and_levels() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(TextureKey::new(1, 0), 1, 1, 8, 8, 21);
        cache.insert(TextureKey::new(2, 0), 2, 1, 8, 8, 21);
        cache.insert(TextureKey::new(1, 1), 3, 1, 4, 4, 21);
        assert!(cache.lookup(TextureKey::new(1, 0), 1, false, 8, 8, 21));
        assert!(cache.lookup(TextureKey::new(2, 0), 1, false, 8, 8, 21));
        assert!(cache.lookup(TextureKey::new(1, 1), 1, false, 4, 4, 21));
        assert_eq!(cache.len(), 3);
    }

    #[test]
    fn open_lock_forces_miss_even_when_unchanged() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(key(7), 10, 3, 64, 32, 21);
        assert!(!cache.lookup(key(7), 3, true, 64, 32, 21));
        assert_eq!(cache.misses, 1);
    }

    #[test]
    fn release_invalidates_every_level_of_the_texture() {
        let mut cache: TextureCache<u32> = TextureCache::new(8);
        cache.insert(TextureKey::new(7, 0), 1, 1, 8, 8, 21);
        cache.insert(TextureKey::new(7, 1), 2, 1, 4, 4, 21);
        cache.insert(TextureKey::new(8, 0), 3, 1, 8, 8, 21);
        assert_eq!(cache.remove_texture(7), 2);
        assert!(!cache.lookup(TextureKey::new(7, 0), 1, false, 8, 8, 21));
        assert!(cache.lookup(TextureKey::new(8, 0), 1, false, 8, 8, 21));
    }

    #[test]
    fn capacity_evicts_least_recently_seen() {
        let mut cache: TextureCache<u32> = TextureCache::new(2);
        cache.insert(key(1), 1, 1, 4, 4, 21);
        cache.insert(key(2), 2, 1, 4, 4, 21);
        // Refresh key 1 so key 2 is the LRU.
        assert!(cache.lookup(key(1), 1, false, 4, 4, 21));
        cache.insert(key(3), 3, 1, 4, 4, 21);
        assert!(cache.resource(key(2)).is_none());
        assert!(cache.resource(key(1)).is_some());
        assert!(cache.resource(key(3)).is_some());
    }
}
