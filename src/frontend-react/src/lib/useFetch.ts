/**
 * Tiny GET-on-mount hook over lib/api (sends the bearer token, handles 401).
 * Returns { data, loading, error, reload }. Pass null to skip fetching.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'

export function useFetch<T = unknown>(path: string | null) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(!!path)
  const [error, setError] = useState('')
  // Monotonic request id — a response only lands if no newer request started
  // since (path can change mid-flight, e.g. switching drawer rows).
  const seq = useRef(0)

  const reload = useCallback(async () => {
    if (!path) {
      setLoading(false)
      return
    }
    const id = ++seq.current
    setLoading(true)
    setError('')
    try {
      const result = await api<T>(path)
      if (id === seq.current) setData(result)
    } catch (e) {
      if (id === seq.current) setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      if (id === seq.current) setLoading(false)
    }
  }, [path])

  useEffect(() => {
    reload()
  }, [reload])

  return { data, loading, error, reload }
}
