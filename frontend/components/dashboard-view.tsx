"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Loader2, SearchX, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AnalyzeInput } from "@/components/analyze-input";
import { InsightCard } from "@/components/insight-card";
import { ModeChips, type ModeFilter } from "@/components/mode-chips";
import { SearchBar } from "@/components/search-bar";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { fetchCreators, fetchInsights, toUserMessage } from "@/lib/api";
import type { Creator } from "@/lib/types";

const PAGE_SIZE = 12;

function GridSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="border-border space-y-3 rounded-xl border p-4">
          <div className="flex justify-between">
            <Skeleton className="h-5 w-16 rounded-full" />
            <Skeleton className="h-4 w-20" />
          </div>
          <Skeleton className="h-5 w-3/4" />
          <Skeleton className="h-3 w-1/2" />
          <div className="space-y-2 pt-2">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="border-border/60 flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-16 text-center">
      <div className="flex size-12 items-center justify-center rounded-full bg-emerald-500/10">
        <Sparkles className="size-6 text-emerald-400" aria-hidden />
      </div>
      <div className="space-y-1">
        <h2 className="font-medium">Nessun insight, per ora</h2>
        <p className="text-muted-foreground max-w-md text-sm">
          Incolla qui sopra il link di un Reel, TikTok o Short: Picox lo analizza ed estrae
          sintesi, stile e script inverso. Oppure aggiungi un creator da monitorare.
        </p>
      </div>
    </div>
  );
}

function NoResultsState({ onReset }: { onReset: () => void }) {
  return (
    <div className="border-border/60 flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-16 text-center">
      <div className="bg-muted flex size-12 items-center justify-center rounded-full">
        <SearchX className="text-muted-foreground size-6" aria-hidden />
      </div>
      <div className="space-y-1">
        <h2 className="font-medium">Nessun risultato</h2>
        <p className="text-muted-foreground max-w-md text-sm">
          Nessun insight corrisponde a questi filtri. Prova con un altro termine o azzera la
          ricerca.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onReset}>
        Azzera i filtri
      </Button>
    </div>
  );
}

export function DashboardView({ sharedUrl }: { sharedUrl?: string }) {
  const [searchInput, setSearchInput] = useState("");
  const [mode, setMode] = useState<ModeFilter>(undefined);

  // L'input resta reattivo; solo il valore stabilizzato raggiunge il backend.
  const search = useDebouncedValue(searchInput, 400);

  const insightsQuery = useInfiniteQuery({
    // I filtri fanno parte della chiave: cambiandoli si ottiene una lista nuova
    // invece di accodare risultati eterogenei a quelli già caricati.
    queryKey: ["insights", { search, mode }],
    initialPageParam: 1,
    queryFn: ({ pageParam, signal }) =>
      fetchInsights({ search, mode, page: pageParam, limit: PAGE_SIZE }, signal),
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.page + 1 : undefined),
  });

  // I creator servono solo a risolvere `creator_id` → username: `/insights` non
  // restituisce l'handle. Cambiano di rado, quindi la cache è lunga.
  const creatorsQuery = useQuery({
    queryKey: ["creators"],
    queryFn: ({ signal }) => fetchCreators(signal),
    staleTime: 5 * 60_000,
  });

  const creatorsById = useMemo(() => {
    const map = new Map<string, Creator>();
    for (const creator of creatorsQuery.data?.items ?? []) {
      map.set(creator.id, creator);
    }
    return map;
  }, [creatorsQuery.data]);

  const insights = useMemo(
    () => insightsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [insightsQuery.data],
  );

  const total = insightsQuery.data?.pages[0]?.total ?? 0;
  const hasFilters = search.trim().length > 0 || mode !== undefined;

  const resetFilters = useCallback(() => {
    setSearchInput("");
    setMode(undefined);
  }, []);

  // Infinite scroll: una sentinella in fondo alla griglia chiede la pagina
  // successiva quando entra nel viewport.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { fetchNextPage, hasNextPage, isFetchingNextPage } = insightsQuery;

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasNextPage) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetchingNextPage) {
          void fetchNextPage();
        }
      },
      { rootMargin: "400px" },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6">
      <section className="space-y-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">I tuoi insight</h1>
          <p className="text-muted-foreground text-sm">
            Analizza un video o cerca fra quelli già in archivio.
          </p>
        </div>

        <AnalyzeInput initialUrl={sharedUrl} />
      </section>

      <section className="space-y-3">
        <SearchBar value={searchInput} onChange={setSearchInput} />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <ModeChips value={mode} onChange={setMode} />
          {insightsQuery.isSuccess && total > 0 && (
            <p className="text-muted-foreground text-sm" aria-live="polite">
              {total} {total === 1 ? "insight" : "insight"}
            </p>
          )}
        </div>
      </section>

      <section>
        {insightsQuery.isPending ? (
          <GridSkeleton />
        ) : insightsQuery.isError ? (
          <div className="border-destructive/30 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border px-6 py-12 text-center">
            <AlertTriangle className="size-6 text-red-400" aria-hidden />
            <div className="space-y-1">
              <h2 className="font-medium">Non riesco a caricare gli insight</h2>
              <p className="text-muted-foreground max-w-md text-sm">
                {toUserMessage(insightsQuery.error)}
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={() => void insightsQuery.refetch()}>
              Riprova
            </Button>
          </div>
        ) : insights.length === 0 ? (
          // Due stati distinti: "non hai ancora nulla" è un onboarding,
          // "nessun risultato" è un vicolo cieco da cui si esce azzerando.
          hasFilters ? (
            <NoResultsState onReset={resetFilters} />
          ) : (
            <EmptyState />
          )
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {insights.map((insight) => (
                <InsightCard
                  key={insight.id}
                  insight={insight}
                  creator={insight.creator_id ? creatorsById.get(insight.creator_id) : undefined}
                  onKeywordClick={setSearchInput}
                />
              ))}
            </div>

            <div ref={sentinelRef} aria-hidden className="h-px" />

            {isFetchingNextPage && (
              <p
                className="text-muted-foreground flex items-center justify-center gap-2 py-6 text-sm"
                role="status"
              >
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Carico altri insight…
              </p>
            )}

            {!hasNextPage && insights.length > PAGE_SIZE && (
              <p className="text-muted-foreground py-6 text-center text-sm">
                Hai visto tutti gli insight.
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
