/**
 * Tiny GET-on-mount hook over lib/api (sends the bearer token, handles 401).
 * Returns { data, loading, error, reload }. Pass null to skip fetching.
 */

import { useCallback, useEffect, useState } from 'react'
import { api } from './api'

export function useFetch<T = unknown>(path: string | null) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    if (!path) return
    setLoading(true)
    setError('')
    try {
      setData(await api<T>(path))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [path])

  useEffect(() => {
    reload()
  }, [reload])

  return { data, loading, error, reload }
}
