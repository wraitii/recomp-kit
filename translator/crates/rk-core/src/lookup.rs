use std::borrow::Borrow;
use std::hash::Hash;

#[allow(clippy::disallowed_types)]
type Map<K, V> = rustc_hash::FxHashMap<K, V>;
#[allow(clippy::disallowed_types)]
type Set<K> = rustc_hash::FxHashSet<K>;

#[derive(Clone, Debug)]
pub struct Lookup<K, V>(Map<K, V>);

impl<K: Eq + Hash, V> Default for Lookup<K, V> {
    fn default() -> Self {
        Self(Map::default())
    }
}

impl<K: Eq + Hash, V> Lookup<K, V> {
    pub fn with_capacity(capacity: usize) -> Self {
        Self(Map::with_capacity_and_hasher(capacity, Default::default()))
    }

    pub fn get<Q: ?Sized + Eq + Hash>(&self, key: &Q) -> Option<&V>
    where
        K: Borrow<Q>,
    {
        self.0.get(key)
    }

    pub fn get_mut<Q: ?Sized + Eq + Hash>(&mut self, key: &Q) -> Option<&mut V>
    where
        K: Borrow<Q>,
    {
        self.0.get_mut(key)
    }

    pub fn contains_key<Q: ?Sized + Eq + Hash>(&self, key: &Q) -> bool
    where
        K: Borrow<Q>,
    {
        self.0.contains_key(key)
    }

    pub fn insert(&mut self, key: K, value: V) -> Option<V> {
        self.0.insert(key, value)
    }

    pub fn remove<Q: ?Sized + Eq + Hash>(&mut self, key: &Q) -> Option<V>
    where
        K: Borrow<Q>,
    {
        self.0.remove(key)
    }

    pub fn entry(&mut self, key: K) -> std::collections::hash_map::Entry<'_, K, V> {
        self.0.entry(key)
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl<K: Eq + Hash, V> FromIterator<(K, V)> for Lookup<K, V> {
    fn from_iter<I: IntoIterator<Item = (K, V)>>(iter: I) -> Self {
        Self(iter.into_iter().collect())
    }
}

#[derive(Clone, Debug)]
pub struct LookupSet<K>(Set<K>);

impl<K: Eq + Hash> Default for LookupSet<K> {
    fn default() -> Self {
        Self(Set::default())
    }
}

impl<K: Eq + Hash> LookupSet<K> {
    pub fn contains<Q: ?Sized + Eq + Hash>(&self, key: &Q) -> bool
    where
        K: Borrow<Q>,
    {
        self.0.contains(key)
    }

    pub fn insert(&mut self, key: K) -> bool {
        self.0.insert(key)
    }

    pub fn remove<Q: ?Sized + Eq + Hash>(&mut self, key: &Q) -> bool
    where
        K: Borrow<Q>,
    {
        self.0.remove(key)
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl<K: Eq + Hash> FromIterator<K> for LookupSet<K> {
    fn from_iter<I: IntoIterator<Item = K>>(iter: I) -> Self {
        Self(iter.into_iter().collect())
    }
}
