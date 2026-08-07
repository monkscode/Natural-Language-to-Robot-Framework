import { useState } from 'react'
import PendingTab from './access/PendingTab'
import MembersTab from './access/MembersTab'
import OrgsTab from './access/OrgsTab'

type Tab = 'pending' | 'members' | 'orgs'

const TABS: { key: Tab; label: string }[] = [
  { key: 'pending', label: 'Pending' },
  { key: 'members', label: 'Members' },
  { key: 'orgs', label: 'Orgs' },
]

export default function AccessConsolePage() {
  const [tab, setTab] = useState<Tab>('pending')
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Access console</h1>
      <div className="flex gap-1 border-b">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${
              tab === t.key
                ? 'border-primary font-medium'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'pending' && <PendingTab />}
      {tab === 'members' && <MembersTab />}
      {tab === 'orgs' && <OrgsTab />}
    </div>
  )
}
