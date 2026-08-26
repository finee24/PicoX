"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useRef, useState } from "react";
import { Loader2, MailCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TurnstileWidget, type TurnstileHandle } from "@/components/turnstile-widget";
import { safeInternalPath } from "@/lib/safe-redirect";
import { getSupabaseBrowserClient } from "@/lib/supabase-client";

type Mode = "login" | "register";

interface AuthFormProps {
  mode: Mode;
  /** Percorso a cui tornare dopo l'accesso, propagato da `proxy.ts`. */
  redirectTo?: string;
  /** `true` quando si arriva qui perché la sessione è scaduta. */
  expired?: boolean;
}

/**
 * I messaggi di GoTrue arrivano in inglese e con formulazioni che non hanno
 * senso per un utente finale. Si traducono i casi ricorrenti e si lascia
 * passare il resto, invece di mostrare un generico "errore" che nasconde
 * informazioni utili.
 */
function translateAuthError(message: string): string {
  const normalized = message.toLowerCase();

  if (normalized.includes("invalid login credentials")) {
    return "Email o password non corretti.";
  }
  if (normalized.includes("email not confirmed")) {
    return "Devi prima confermare l'indirizzo email. Controlla la posta.";
  }
  if (normalized.includes("user already registered")) {
    return "Esiste già un account con questa email. Prova ad accedere.";
  }
  if (normalized.includes("password should be at least")) {
    return "La password è troppo corta: servono almeno 6 caratteri.";
  }
  if (normalized.includes("unable to validate email address")) {
    return "L'indirizzo email non sembra valido.";
  }
  if (normalized.includes("rate limit") || normalized.includes("too many requests")) {
    return "Troppi tentativi. Attendi qualche minuto e riprova.";
  }
  if (normalized.includes("captcha")) {
    // Il testo di GoTrue («request disallowed (no captcha_token found)») è una
    // descrizione della nostra configurazione, non un'istruzione per chi legge.
    return "Verifica antispam non superata. Attendi che si completi qui sotto e riprova.";
  }
  if (normalized.includes("fetch") || normalized.includes("network")) {
    return "Impossibile raggiungere Supabase. Verifica la connessione.";
  }
  return message;
}

export function AuthForm({ mode, redirectTo, expired }: AuthFormProps) {
  const router = useRouter();
  const isRegister = mode === "register";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmationSent, setConfirmationSent] = useState(false);
  // Il captcha vale per **ogni** percorso di autenticazione, non solo per la
  // registrazione.
  //
  // La versione precedente lo montava solo qui, con questo argomento: l'accesso
  // non crea account, quindi non alimenta il vettore Sybil della voce A4. Giusto
  // sul rischio, sbagliato sul meccanismo — la protezione di Supabase Auth si
  // abilita **per progetto, non per endpoint**, quindi accenderla per il signup
  // la impone anche a `signInWithPassword`. Senza token GoTrue rifiuta con
  // «request disallowed (no captcha_token found)», e l'accesso diventa
  // impossibile per tutti.
  //
  // Non se n'era accorto nessuno perché l'unica sessione mai creata veniva
  // dall'accesso automatico che segue la registrazione, che il token lo manda.
  // Lo stesso varrà per il recupero password il giorno in cui esisterà: oggi
  // non c'è alcun `resetPasswordForEmail` nel progetto, e va scritto con il
  // token fin dalla prima riga invece di aggiungercelo dopo.
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const turnstileRef = useRef<TurnstileHandle>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    // Fermarsi qui invece di lasciar partire la richiesta: senza token Supabase
    // risponderebbe comunque con un errore di captcha, ma tradotto per l'utente
    // suonerebbe come un guasto invece che come "manca un passaggio".
    if (!captchaToken) {
      setError("Completa la verifica antispam qui sotto per continuare.");
      return;
    }

    setPending(true);

    const supabase = getSupabaseBrowserClient();

    try {
      if (isRegister) {
        const { data, error: signUpError } = await supabase.auth.signUp({
          email,
          password,
          options: {
            emailRedirectTo: `${window.location.origin}/auth/callback`,
            // Nome del parametro verificato sui tipi di @supabase/auth-js
            // 2.112.2 installato (`SignUpWithPasswordCredentials`), non preso
            // dalla documentazione generica.
            captchaToken: captchaToken ?? undefined,
          },
        });

        if (signUpError) {
          setError(translateAuthError(signUpError.message));
          return;
        }

        // Con la conferma via email attiva, `session` è null finché l'utente
        // non clicca il link: non c'è nessuna dashboard da mostrare ancora.
        if (!data.session) {
          setConfirmationSent(true);
          return;
        }
      } else {
        const { error: signInError } = await supabase.auth.signInWithPassword({
          email,
          password,
          // Stessa forma del signup qui sopra, e verificata sugli stessi tipi:
          // `SignInWithPasswordCredentials` di @supabase/auth-js 2.112.2
          // dichiara `options.captchaToken`.
          options: { captchaToken: captchaToken ?? undefined },
        });

        if (signInError) {
          setError(translateAuthError(signInError.message));
          return;
        }
      }

      // `refresh()` prima di `push()`: rilegge i cookie di sessione lato server,
      // altrimenti il proxy vedrebbe ancora lo stato precedente e rimanderebbe
      // al login.
      router.refresh();
      // La destinazione arriva dalla query string: va normalizzata, altrimenti
      // `?redirect=//evil.com` porterebbe fuori dominio proprio dopo un login
      // riuscito. Vedi `lib/safe-redirect.ts`.
      router.push(safeInternalPath(redirectTo));
    } catch (caught) {
      // Eccezione non prevista: il dettaglio resta in console, all'utente va un
      // messaggio nostro. `translateAuthError` fa da passthrough sui casi non
      // riconosciuti, e qui non sappiamo cosa contenga il testo originale.
      console.error("Errore inatteso durante l'autenticazione:", caught);
      setError("Si è verificato un errore imprevisto. Riprova.");
    } finally {
      setPending(false);
      // Dopo OGNI tentativo, riuscito o fallito. Un token Turnstile vale una
      // volta sola: senza reset il secondo invio ne manderebbe uno gia' speso e
      // Supabase lo rifiuterebbe, mentre all'utente il widget resterebbe
      // spuntato di verde — un fallimento senza nulla che lo spieghi.
      turnstileRef.current?.reset();
    }
  }

  if (confirmationSent) {
    return (
      <div className="space-y-4 text-center">
        <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-emerald-500/10">
          <MailCheck className="size-6 text-emerald-400" aria-hidden />
        </div>
        <div className="space-y-1">
          <h2 className="text-lg font-medium">Controlla la tua email</h2>
          <p className="text-muted-foreground text-sm">
            Abbiamo inviato un link di conferma a{" "}
            <span className="text-foreground font-medium">{email}</span>. Aprilo per
            attivare l&apos;account.
          </p>
        </div>
        <Button variant="outline" className="w-full" asChild>
          <Link href="/login">Torna all&apos;accesso</Link>
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" noValidate>
      {expired && !error && (
        <p
          role="status"
          className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300"
        >
          La sessione è scaduta. Accedi di nuovo per continuare.
        </p>
      )}

      <div className="space-y-2">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="tu@esempio.it"
          disabled={pending}
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete={isRegister ? "new-password" : "current-password"}
          required
          minLength={6}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={isRegister ? "Almeno 6 caratteri" : "••••••••"}
          disabled={pending}
        />
      </div>

      <TurnstileWidget ref={turnstileRef} onToken={setCaptchaToken} />

      {error && (
        <p
          role="alert"
          className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300"
        >
          {error}
        </p>
      )}

      <Button type="submit" className="w-full" disabled={pending}>
        {pending && <Loader2 className="size-4 animate-spin" aria-hidden />}
        {isRegister ? "Crea account" : "Accedi"}
      </Button>

      <p className="text-muted-foreground text-center text-sm">
        {isRegister ? (
          <>
            Hai già un account?{" "}
            <Link href="/login" className="text-emerald-400 underline-offset-4 hover:underline">
              Accedi
            </Link>
          </>
        ) : (
          <>
            Non hai un account?{" "}
            <Link
              href="/register"
              className="text-emerald-400 underline-offset-4 hover:underline"
            >
              Registrati
            </Link>
          </>
        )}
      </p>
    </form>
  );
}
