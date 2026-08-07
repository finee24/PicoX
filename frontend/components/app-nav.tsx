"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, LogOut, Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PicoxMark } from "@/components/picox-mark";
import { getSupabaseBrowserClient } from "@/lib/supabase-client";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/dashboard", label: "Insight", icon: LayoutGrid },
  { href: "/creators", label: "Creator", icon: Users },
] as const;

export function AppNav() {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();

  async function handleSignOut() {
    await getSupabaseBrowserClient().auth.signOut();
    // La cache contiene dati dell'utente che sta uscendo: senza questo, chi
    // accede subito dopo sullo stesso tab vedrebbe gli insight del precedente
    // finché le query non si invalidano da sole.
    queryClient.clear();
    router.refresh();
    router.push("/login");
  }

  return (
    <header className="border-border/60 bg-background/80 sticky top-0 z-30 border-b backdrop-blur">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-4 px-4">
        <Link href="/dashboard" className="flex items-center gap-2">
          <PicoxMark className="size-7" />
          <span className="font-semibold tracking-tight">Picox</span>
        </Link>

        <nav className="flex items-center gap-1" aria-label="Navigazione principale">
          {LINKS.map(({ href, label, icon: Icon }) => {
            const active = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/50",
                )}
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </Link>
            );
          })}
        </nav>

        <Button
          variant="ghost"
          size="sm"
          onClick={handleSignOut}
          className="text-muted-foreground hover:text-foreground ml-auto"
        >
          <LogOut className="size-4" aria-hidden />
          <span className="sr-only sm:not-sr-only">Esci</span>
        </Button>
      </div>
    </header>
  );
}
