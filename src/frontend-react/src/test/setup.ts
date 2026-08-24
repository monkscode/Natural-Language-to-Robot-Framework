/**
 * Vitest setup — runs once per test file, before any test.
 *
 * Registers @testing-library/jest-dom's DOM matchers (toBeInTheDocument and
 * friends) on vitest's `expect`, and unmounts every rendered tree after each
 * test so one test's component cannot answer another test's query.
 */
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())
