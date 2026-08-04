import { motion } from 'motion/react';
import { Bot, Monitor } from 'lucide-react';

interface Props {
  enabled: boolean;
  onToggle: (enabled: boolean) => void;
  available: boolean;
}

export default function AgentToggle({ enabled, onToggle, available }: Props) {
  return (
    <button
      type="button"
      onClick={() => available && onToggle(!enabled)}
      disabled={!available}
      aria-pressed={enabled}
      /* Ink, not a brand blue: this switches a mode, it does not report a
         measurement, and colour in this interface is reserved for measured
         values. */
      className={`relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-all select-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
        !available
          ? 'opacity-40 cursor-not-allowed bg-muted/30 border-transparent text-muted-foreground'
          : enabled
            ? 'bg-foreground border-foreground text-background'
            : 'bg-muted border-transparent text-foreground hover:bg-muted/70'
      }`}
      title={!available ? 'Agent not available' : enabled ? 'Switch to Standard mode' : 'Switch to Agent mode'}
    >
      {/* Pill toggle track */}
      <span
        className={`relative inline-flex h-4 w-7 flex-shrink-0 rounded-full transition-colors duration-200 ${
          enabled ? 'bg-white/30' : 'bg-foreground/15'
        }`}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 500, damping: 30 }}
          className={`absolute top-0.5 h-3 w-3 rounded-full ${
            enabled ? 'bg-white left-[13px]' : 'bg-foreground/50 left-[2px]'
          }`}
        />
      </span>

      {/* Icon + label */}
      {enabled ? (
        <Bot className="w-3.5 h-3.5" />
      ) : (
        <Monitor className="w-3.5 h-3.5" />
      )}
      <span>{enabled ? 'Agent' : 'Standard'}</span>
    </button>
  );
}
