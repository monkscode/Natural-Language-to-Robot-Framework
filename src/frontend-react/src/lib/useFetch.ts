/**
 * Tiny GET-on-mount hook over lib/api (sends the bearer token, handles 401).
 * Returns { data, loading, error, reload }. Pass null to skip fetching.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from './api'

export function useFetch<T = unknown>(path: string | null) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(!!path)
  const [error, setError] = useState('')
  // Monotonic request id — a response only lands if no newer request started
  // since (path can change mid-flight, e.g. switching drawer rows).
  const seq = useRef(0)
  // The path `data` is currently an answer TO. Not state: nothing renders it,
  // it is only read on the failure path below, and it is written in the same
  // place `data` is.
  const dataPath = useRef<string | null>(null)
  // The path the hook holds NOW. A reload() outlives its render: a caller that
  // awaits an action and then calls reload() gets the one from the render that
  // started the action. If the path moved meanwhile, that reload() names a
  // path nobody is looking at, and as the newest request its answer would land
  // over the current path's — so it does nothing instead. Updated in a LAYOUT
  // effect, which runs inside the commit: a plain effect runs later (after
  // paint, for an update that is not a click), and a reload() that fired in
  // between found the old path here and refetched it. Not written during
  // render either: a render React throws away would leave it naming a path
  // that never committed, and the committed path's own reload() would then do
  // nothing.
  const latestPath = useRef(path)
  useLayoutEffect(() => { latestPath.current = path }, [path])

  const reload = useCallback(async () => {
    if (path !== latestPath.current) return
    if (!path) {
      setLoading(false)
      return
    }
    const id = ++seq.current
    setLoading(true)
    setError('')
    try {
      const result = await api<T>(path)
      if (id === seq.current) {
        setData(result)
        dataPath.current = path
      }
    } catch (e) {
      if (id === seq.current) {
        setError(e instanceof Error ? e.message : 'Failed to load')
        // `data` is deliberately KEPT when this path's own refetch fails —
        // that is the last good view of the same screen, and most callers
        // render it under an error banner on purpose.
        //
        // It must NOT be kept when it belongs to a different path. Then it is
        // not a stale view of this request, it is another request's answer,
        // and callers that render `{error && …}` and `{data && …}` as
        // siblings (LearningPage's hints and runs tables, TriggersTab's list —
        // each with a path built from a filter that changes while mounted)
        // would put the banner above the PREVIOUS filter's rows and its
        // previous total.
        //
        // Scoped to the failure path on purpose: clearing on every path change
        // would also fire on the SUCCESS path, defeating the
        // `{loading && !data && …}` keep-previous-data guard those same
        // callers rely on.
        if (dataPath.current !== path) {
          setData(null)
          dataPath.current = null
        }
      }
    } finally {
      if (id === seq.current) setLoading(false)
    }
  }, [path])

  useEffect(() => {
    reload()
  }, [reload])

  return { data, loading, error, reload }
}
