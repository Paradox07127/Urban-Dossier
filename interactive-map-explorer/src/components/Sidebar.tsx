import { Search, MapPin, Navigation, Info, SortAsc } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { motion } from 'motion/react';
import { Location } from '../types';

interface SidebarProps {
  locations: Location[];
  onLocationSelect: (location: Location) => void;
  searchQuery: string;
  onSearchChange: (query: string) => void;
  sortBy: keyof Location['scores'];
  onSortChange: (sort: keyof Location['scores']) => void;
}

const SORT_OPTIONS: { label: string; value: keyof Location['scores'] }[] = [
  { label: 'Overall', value: 'overall' },
  { label: 'Amenities', value: 'amenities' },
  { label: 'Safety', value: 'safety' },
  { label: 'Transit', value: 'transit' },
];

export default function Sidebar({
  locations,
  onLocationSelect,
  searchQuery,
  onSearchChange,
  sortBy,
  onSortChange,
}: SidebarProps) {
  return (
    <div className="flex flex-col h-full bg-background border-r border-border w-80 md:w-96 shadow-xl z-10">
      <div className="p-6 space-y-4">
        <div className="flex items-center gap-2">
          <div className="p-2 bg-primary rounded-lg">
            <Navigation className="w-5 h-5 text-primary-foreground" />
          </div>
          <h1 className="text-xl font-bold tracking-tight">Urban Dossier NYC</h1>
        </div>
        
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Search address or category..."
            className="pl-9 bg-muted/50 border-none focus-visible:ring-1"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center gap-2 ud-label">
            <SortAsc className="w-3 h-3" />
            Sort by Standard
          </div>
          <div className="flex flex-wrap gap-1.5">
            {SORT_OPTIONS.map((option) => (
              <button
                key={option.value}
                onClick={() => onSortChange(option.value)}
                className={`px-2 py-1 rounded-md text-[10px] font-medium transition-all ${
                  sortBy === option.value
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'bg-muted text-muted-foreground hover:bg-muted/80'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <Separator />

      <ScrollArea className="flex-1">
        <div className="p-4 space-y-3">
          {locations.length > 0 ? (
            locations.map((location, index) => (
              <motion.div
                key={location.id}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: index * 0.05 }}
              >
                <Card
                  className="cursor-pointer hover:bg-accent/50 transition-colors border border-border/50 shadow-sm bg-muted/20 overflow-hidden group"
                  onClick={() => onLocationSelect(location)}
                >
                  <CardContent className="p-4 space-y-2 relative">
                    <div className="absolute right-0 top-0 h-full w-1 bg-primary opacity-0 group-hover:opacity-100 transition-opacity" />
                    
                    <div className="flex justify-between items-start">
                      <h3 className="font-semibold text-sm leading-tight max-w-[70%]">{location.title}</h3>
                      <div className="flex flex-col items-end gap-1">
                        <Badge variant="secondary" className="text-[9px] px-1.5 py-0 h-4">
                          {location.category}
                        </Badge>
                        <div className="text-xs font-bold text-primary">
                          {location.scores[sortBy]}
                          <span className="text-[8px] font-normal text-muted-foreground ml-0.5">pts</span>
                        </div>
                      </div>
                    </div>
                    
                    <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
                      {location.description}
                    </p>
                    
                    <div className="flex items-center justify-between pt-1">
                      <div className="flex items-center gap-1 text-[9px] text-muted-foreground">
                        <MapPin className="w-3 h-3" />
                        <span>{location.position[0].toFixed(3)}, {location.position[1].toFixed(3)}</span>
                      </div>
                      <div className="flex gap-0.5">
                        {Object.entries(location.scores).filter(([k]) => k !== 'overall').map(([key, val]) => (
                          <div 
                            key={key} 
                            className={`w-1.5 h-1.5 rounded-full ${
                              (val as number) > 80 ? 'bg-green-500' : (val as number) > 60 ? 'bg-yellow-500' : 'bg-red-500'
                            }`}
                            title={`${key}: ${val}`}
                          />
                        ))}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </motion.div>
            ))
          ) : (
            <div className="flex flex-col items-center justify-center py-12 text-center space-y-2">
              <Info className="w-8 h-8 text-muted-foreground/50" />
              <p className="text-sm text-muted-foreground">No locations found</p>
            </div>
          )}
        </div>
      </ScrollArea>

      <div className="p-4 bg-muted/20 border-t border-border">
        <p className="ud-label text-center">
          NYC Open Data • Local AI Analysis
        </p>
      </div>
    </div>
  );
}

