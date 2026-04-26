import { useState } from 'react';
import { motion } from 'motion/react';
import { MessageSquare, FileText, Image } from 'lucide-react';
import AgentChat from './AgentChat';
import AgentReport from './AgentReport';

interface Props {
  sessionId: string | null;
  onCreateSession: () => Promise<string>;
  analysisPayload: any;
}

const TABS = [
  { id: 'chat' as const, label: 'Chat', icon: MessageSquare },
  { id: 'report' as const, label: 'Report', icon: FileText },
  { id: 'poster' as const, label: 'Poster', icon: Image },
] as const;

type TabId = (typeof TABS)[number]['id'];

export default function AgentPanel({ sessionId, onCreateSession, analysisPayload }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('chat');

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Tab bar */}
      <div className="flex items-center border-b border-border/60 px-4">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={`relative flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors ${
                isActive ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              <span>{tab.label}</span>
              {isActive && (
                <motion.div
                  layoutId="agent-tab-underline"
                  className="absolute bottom-0 left-2 right-2 h-0.5 bg-primary rounded-full"
                  transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                />
              )}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0">
        {activeTab === 'chat' && (
          <AgentChat
            sessionId={sessionId}
            analysisPayload={analysisPayload}
            onCreateSession={onCreateSession}
          />
        )}
        {activeTab === 'report' && (
          <AgentReport
            sessionId={sessionId}
            onCreateSession={onCreateSession}
            mode="report"
          />
        )}
        {activeTab === 'poster' && (
          <AgentReport
            sessionId={sessionId}
            onCreateSession={onCreateSession}
            mode="poster"
          />
        )}
      </div>
    </div>
  );
}
