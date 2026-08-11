"use client";

import { Search, X } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export function SearchBar({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="relative">
      <Search
        className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        aria-hidden
      />
      <label htmlFor="insight-search" className="sr-only">
        Cerca fra i tuoi insight
      </label>
      <Input
        id="insight-search"
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Cerca per keyword o contenuto…"
        // Stesso limite del backend: `list_insights` dichiara
        // `Query(max_length=200)` e oltre risponde 422. Senza, incollare un
        // testo lungo mostrava un errore generico invece di fermarsi al limite.
        // Il backend tronca poi a 100 caratteri per la ricerca vera
        // (`_SEARCH_MAX_LENGTH`): qui conta la soglia che genera l'errore.
        maxLength={200}
        className="pr-10 pl-9"
      />
      {value && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => onChange("")}
          className="absolute top-1/2 right-1 size-7 -translate-y-1/2"
          aria-label="Cancella la ricerca"
        >
          <X className="size-4" aria-hidden />
        </Button>
      )}
    </div>
  );
}
