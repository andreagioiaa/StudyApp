# StudyFlow - Gestionale Studio & Analytics Desktop

Applicazione desktop Windows per organizzare lo studio con il metodo Pomodoro,
bloccare le distrazioni, importare il calendario Google, gestire materiali e PDF,
trascrivere videolezioni e analizzare la qualita' dell'attenzione.

L'interfaccia e' realizzata in **CustomTkinter**, i dati sono salvati in un
database **SQLite** locale e l'eseguibile finale e' un singolo file prodotto con
**PyInstaller**. Non vengono usati Electron, Chromium, server web o servizi cloud
per l'elaborazione o la sincronizzazione dei dati.

---

## 1. Funzionalita'

### Pomodoro
- Fasi configurabili: lavoro (default 25 min), pausa breve (5 min), pausa lunga
  (15 min) e numero di cicli prima della pausa lunga (default 4).
- Avvio, pausa/ripresa, salto fase e reset.
- Avanzamento automatico opzionale alla fase successiva.
- Timer basato su clock monotonic in un thread dedicato: continua a funzionare
  mentre si naviga tra le sezioni.
- Salvataggio dei minuti effettivi sia per i Pomodori completati sia per quelli
  interrotti.
- Associazione della sessione a materia, materiale e/o evento del calendario.
- Notifica sonora di fine fase tramite `winsound`.

### Blocco delle distrazioni
- **Siti web** tramite una sezione gestita del file `hosts` di Windows.
- **Applicazioni** tramite `psutil`: rilevamento e, se abilitata, terminazione dei
  processi in blacklist.
- Ogni tentativo rilevato viene registrato in `distraction_attempts` con target,
  timestamp e ID della sessione Pomodoro.
- Blacklist modificabile dall'interfaccia per domini e nomi processo.
- Intercettazione best-effort dei tentativi HTTP/HTTPS tramite listener loopback
  sulle porte 80 e 443 (`Host` HTTP e SNI TLS).
- Ripristino automatico del file `hosts` alla fine della sessione, alla chiusura
  dell'app, in caso di crash o di heartbeat assente.

### Calendario
- Importazione di file `.ics` esportati da Google Calendar.
- Gestione di eventi normali, eventi ricorrenti (`RRULE`, `RDATE`, `EXDATE`),
  eventi tutto il giorno e fusi orari.
- Agenda giornaliera e settimanale.
- Avvio di un Pomodoro direttamente da un evento, con durata proposta in base
  all'evento.
- Svuotamento del calendario importato.
- Calendari Google in sola lettura tramite **indirizzo segreto iCal**, senza
  API ne' login (vedi sezione 8), con materia e colore per calendario.
- In alternativa, sincronizzazione bidirezionale con Google Calendar (OAuth 2.0 PKCE): un
  calendario Google per materia, colori Google, sync automatica all'avvio e
  pulsante **Sincronizza Ora** con indicatore del tempo trascorso, creazione /
  modifica / eliminazione degli eventi da StudyFlow con coda offline.

### Materiali dentro le materie
- I PDF, i documenti e le note non hanno piu' una sezione autonoma: ogni
  materiale vive nella pagina della materia a cui appartiene.
- Registrazione di titolo, file e ore previste direttamente dalla pagina materia.
- Collegamento opzionale a un evento del calendario.
- Avanzamento ore registrate / ore previste con barra di progresso.
- Aggiornamento automatico delle ore quando si studia da un materiale.
- Aggiunta manuale di ore.
- Apertura del file con il programma predefinito di Windows (es. lettore PDF).
- I materiali creati prima di questa versione senza materia restano raggiungibili
  da una voce temporanea **Materiali senza materia**, finche' non vengono
  assegnati.

### Materie, CFU ed esami
- Pagina dedicata per ogni materia con docente, email, CFU, monte ore, data
  d'esame e note facoltative.
- Le materie gia' presenti in materiali, video, flashcard, calendario o sessioni
  compaiono automaticamente, anche senza dettagli compilati.
- Monte ore calcolato secondo la normativa universitaria: **1 CFU = 25 ore** di
  impegno complessivo (D.M. 270/2004).
- Data d'esame definita oppure **non ancora definita**, con countdown aggiornato
  fino al giorno dell'esame.
- Rinomina della materia propagata automaticamente a tutti i dati collegati.
- Eliminazione dei dettagli consentita solo quando non ci sono dati collegati,
  per evitare perdite accidentali.

### Flashcard e ripetizione spaziata
- Archivio di schede domanda/risposta collegate a una materia, con note
  facoltative.
- Filtri per materia e per stato (da ripassare, nuove, apprendimento, ripasso) e
  ricerca libera su domanda, risposta, note e materia.
- Sessione di ripasso dedicata con pulsante **Mostra risposta**, anteprima
  dell'intervallo per ogni giudizio e scorciatoie da tastiera.
- Quattro giudizi: **Ancora**, **Difficile**, **Bene**, **Facile**.
- Pianificazione SM-2 in stile Anki: passi di apprendimento di 1 e 10 minuti,
  intervallo di laurea di 1 giorno, intervallo facile di 4 giorni, fattore di
  facilita' tra 1.3 e 5.0, gestione dei lapse e riapprendimento.
- Storico dei ripassi per ogni scheda, statistiche delle scadenze e ripristino
  di una scheda allo stato nuovo.
- Le schede scadute o mai studiate sono subito disponibili nella sessione
  **Studia ora**.

### Backup e trasferimento dati
- Esportazione di tutti i dati applicativi in un archivio ZIP portabile.
- Importazione con sostituzione completa delle tabelle e creazione automatica di
  un backup di sicurezza prima di modificare il database.
- Le trascrizioni `.txt` sono incluse nell'archivio; i modelli whisper.cpp, i
  video e i file audio non vengono inclusi per mantenere il backup leggero.

### Audio e concentrazione
- Riproduzione locale di file MP3, OGG, WAV e FLAC dalla cartella `sounds`.
- Importazione di file audio dal disco e gestione dei nomi duplicati.
- Download dell'audio da un link YouTube tramite `yt-dlp`: la traccia migliore
  viene convertita in MP3 con FFmpeg e salvata con il titolo del video.
- Se il file esiste gia', l'app propone **Crea Copia**, **Vai a Cartella** o
  **Salta**.
- Avvio automatico della musica con le fasi di lavoro del Pomodoro, pausa
  automatica quando il timer viene messo in pausa, volume e loop configurabili.
- Quattro suoni di concentrazione generati localmente (rumore bianco, rumore
  marrone, pioggia e onde), senza download e senza copyright.
- La riproduzione manuale resta indipendente dal timer.

### Video e Trascrizione
- Archivio locale di videolezioni associato a materia/tag, con filtro e ricerca.
- Estrazione audio con FFmpeg in WAV mono 16 kHz e trascrizione locale tramite
  `whisper.cpp` (`pywhispercpp`), senza servizi cloud.
- Modello `base-q5_1` predefinito (circa 60 MB) o `small-q5_1` opzionale
  (circa 190 MB), scaricato una sola volta.
- Trascrizione in un processo separato, con barra di avanzamento e annullamento.
- File `.txt` UTF-8 con timestamp `[mm:ss]`, nome personalizzabile prima e dopo
  la trascrizione, viewer integrato, apertura e copia del testo.
- Stati e recupero automatico dei job interrotti; il video originale non viene
  copiato ne' modificato.

### PDF e Slide
- Trascrizione di slide e dispense PDF in **.txt**, **.tex** (corpo o documento
  completo compilabile) e **.docx** con equazioni native di Word (OMML).
- Testo vettoriale con PyMuPDF (pdfplumber come alternativa), OCR locale con
  Tesseract o PaddleOCR per i PDF scansionati, Marker o Nougat facoltativi per le
  pagine con formule complesse.
- Ricostruzione delle formule dai PDF LaTeX/Beamer (frazioni, limiti, apici,
  pedici, radici, `\mathbb`, `\vec`) e dai simboli Unicode di PowerPoint/Word.
- Marcatori `=== SLIDE N ===` / `% === Slide N ===`, elenchi puntati e numerati
  annidati, rimozione di intestazioni ripetute, numeri di pagina e sillabazioni.
- Editor con anteprima live affiancata (formule renderizzate in locale), viste
  del LaTeX e del TXT finali, modifica in Markdown o direttamente in LaTeX.
- Singolo file o cartella intera (batch), barra di avanzamento e annullamento,
  copia del LaTeX negli appunti, anteprima KaTeX offline nel browser.
- **Rimuovi testo** (Ctrl+H): toglie il testo ridondante anche dopo
  l'estrazione, come un "sostituisci tutto" con sostituzione vuota, sul
  documento aperto o su tutti; le regole possono essere ricordate e applicate
  alle estrazioni successive.

### Analisi
- KPI per oggi, settimana e mese.
- Quality Score 0-100%.
- Percentuale di Pomodori completati senza distrazioni.
- Numero di distrazioni bloccate.
- Grafici Matplotlib incorporati in CustomTkinter:
  - ore studiate per giorno o per materia;
  - tentativi per fascia oraria o per sorgente.
- Matplotlib viene importato in modo lazy solo alla prima apertura della
  dashboard, per mantenere leggero l'avvio.

### Affidabilita'
- Istanza singola tramite mutex Windows.
- Logging rotante.
- Chiusura ordinata con `atexit`, gestione di `SIGINT`/`SIGTERM` e ripristino
  hosts anche in caso di arresto anomalo.
- Processo guardiano elevato separato, attivato solo quando serve modificare il
  file `hosts`.

---

## 2. Requisiti

- Windows 10 o 11 a 64 bit.
- Python **3.12.x** a 64 bit per l'esecuzione da sorgente e per la build.
- Nessun privilegio amministratore per avviare l'applicazione.
- Connessione a Internet per installare le dipendenze, per la build, per il primo
  download del modello di trascrizione e solo su richiesta dell'utente per
  scaricare l'audio da YouTube.
- **FFmpeg** per il download YouTube e per l'estrazione audio delle videolezioni:
  deve essere nel `PATH`, nella cartella dati di StudyFlow oppure in una cartella
  `ffmpeg` al suo interno. Sul PC di sviluppo e' installato con
  `winget install Gyan.FFmpeg`.
- Per la trascrizione non serve una GPU: il motore `whisper.cpp` usa la CPU. Il
  modello predefinito occupa circa 60 MB e viene scaricato una sola volta.
- Per **PDF e Slide**: PyMuPDF, pdfplumber e pypandoc-binary sono in
  `requirements.txt`. Facoltativi: **Tesseract-OCR** con la lingua italiana
  (`winget install UB-Mannheim.TesseractOCR`) per i PDF scansionati, **PaddleOCR**,
  **Marker** (`pip install marker-pdf`) o **Nougat** (`pip install nougat-ocr`)
  per le formule complesse. Vedi la sezione 11b.
- Facoltativo: **Deno** o **Node.js 22+** per la massima compatibilita' con
  l'estrazione YouTube di `yt-dlp`. Se assenti, alcuni video potrebbero non
  esporre tutti i formati audio.

Dipendenze principali:

| Pacchetto | Versione |
|---|---|
| customtkinter | 5.2.2 |
| psutil | 7.2.2 |
| icalendar | 7.3.0 |
| python-dateutil | 2.9.0.post0 |
| tzdata | 2026.1 |
| matplotlib | 3.11.2 |
| pygame | 2.6.1 |
| yt-dlp | 2026.8.19 |
| yt-dlp-ejs | 0.8.0 |
| pywhispercpp | 1.5.1 |
| numpy | 2.3.2 |
| requests | 2.32.3 |
| tqdm | 4.67.1 |
| platformdirs | 4.3.8 |
| pymupdf | 1.27.2.3 |
| pdfplumber | 0.11.10 |
| pypandoc_binary | 1.17 |
| pyinstaller (solo build) | 6.20.0 |

Le versioni esatte sono in `requirements.txt`. Pygame viene importato solo alla
prima riproduzione, quindi non incide sul consumo a riposo. I modelli GGML di
`whisper.cpp` non sono pacchetti Python e non sono inclusi nell'eseguibile:
vengono scaricati al primo utilizzo nella cartella `models` dei dati.

---

## 3. Avvio da sorgente

Aprire PowerShell nella cartella del progetto:

```powershell
Set-Location D:\StudyApp

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python main.py
```

Se PowerShell blocca l'attivazione dello script:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

Opzioni disponibili:

```powershell
python main.py --version
python main.py --debug
python main.py --data-dir C:\StudyFlowData
```

`--data-dir` e' utile per test o per una modalita' portabile. In alternativa si
puo' impostare la variabile d'ambiente `STUDYFLOW_DATA_DIR`.

---

## 4. Struttura del progetto

```text
StudyApp/
├── main.py                     # entry point, istanza singola, modalita' guard
├── StudyFlow.spec              # build PyInstaller one-file
├── build.ps1                   # venv + dipendenze + test + build
├── requirements.txt            # dipendenze runtime e build
├── version_info.txt            # metadati versione dell'exe
├── README.md
├── assets/
│   ├── app.ico                 # icona Windows
│   ├── app.png
│   └── katex/                  # KaTeX 0.16 locale (MIT) per l'anteprima HTML
├── study_manager/
│   ├── __init__.py
│   ├── config.py               # costanti, default, palette, Quality Score
│   ├── database.py             # schema SQLite, migrazioni e query
│   ├── logging_setup.py        # log rotante
│   ├── paths.py                # cartella dati e risorse in modalita' frozen
│   ├── utils.py                # date, validazione, scrittura atomica
│   ├── services/
│   │   ├── pomodoro.py         # motore timer thread-safe
│   │   ├── pomodoro_service.py # timer + database + blocker
│   │   ├── blocker.py          # orchestrazione blocco siti/app
│   │   ├── hosts_service.py    # sezione gestita del file hosts
│   │   ├── hosts_guard.py      # helper elevato con heartbeat
│   │   ├── guard_client.py     # controllo dell'helper da processo normale
│   │   ├── uac.py              # ShellExecuteW runas, pid, privilegi
│   │   ├── web_trap.py         # listener HTTP Host / TLS SNI
│   │   ├── ics_import.py       # parsing ed espansione ricorrenze ICS
│   │   ├── audio_service.py    # mixer pygame e suoni generati
│   │   ├── youtube_audio.py    # download yt-dlp + conversione FFmpeg
│   │   ├── transcription.py    # whisper.cpp, FFmpeg, modelli e controller
│   │   ├── spaced_repetition.py # pianificazione SM-2/Anki per le flashcard
│   │   ├── subjects.py         # CFU, monte ore, date e countdown esami
│   │   ├── backup.py           # export/import ZIP, validazione e ripristino
│   │   └── pdf_tools/          # PDF e Slide (nessuna dipendenza dalla GUI)
│   │       ├── document.py     # blocchi, formato sorgente con === SLIDE N ===
│   │       ├── extract.py      # PyMuPDF, pdfplumber, OCR, Marker/Nougat
│   │       ├── layout.py       # titoli, elenchi, paragrafi, pulizia margini
│   │       ├── mathlayout.py   # ricostruzione geometrica delle formule
│   │       ├── mathconv.py     # Unicode <-> LaTeX
│   │       ├── export.py       # .txt, .tex, .docx (Pandoc), HTML KaTeX
│   │       ├── text_removal.py # "Rimuovi testo": rimozione e regole salvate
│   │       └── controller.py   # thread di lavoro, batch, annullamento
│   └── ui/
│       ├── app.py              # finestra principale, navigazione, polling
│       ├── pomodoro_view.py
│       ├── audio_view.py       # lettore, libreria, YouTube, suoni
│       ├── video_view.py       # archivio video, trascrizione e viewer .txt
│       ├── pdf_view.py         # PDF e Slide: editor, anteprima live, export
│       ├── remove_text_dialog.py # finestra "Rimuovi testo" (Ctrl+H)
│       ├── math_render.py      # formule -> immagini con matplotlib.mathtext
│       ├── blocker_view.py
│       ├── calendar_view.py
│       ├── materials.py        # materiali dentro la pagina materia e dialogo
│       ├── subjects_view.py    # pagine materia, docente, CFU ed esami
│       ├── flashcard_view.py   # archivio flashcard, dialoghi e sessione di studio
│       ├── analytics_view.py
│       ├── settings_view.py    # impostazioni e backup/importazione dati
│       ├── formatting.py
│       ├── scrolling.py        # rotella fluida e scorrimento annidato
│       ├── theme.py
│       └── widgets.py
└── tests/
    ├── test_audio.py
    ├── test_backup.py
    ├── test_core.py
    ├── test_flashcards.py
    ├── test_pdf_tools.py
    ├── test_pomodoro.py
    ├── test_scrolling.py
    ├── test_subjects.py
    └── test_transcription.py
```

---

## 5. Dove vengono salvati i dati

Cartella predefinita:

```text
%LOCALAPPDATA%\StudyFlow\
```

Contenuto:

| File | Descrizione |
|---|---|
| `study_manager.db` | database SQLite con sessioni, tentativi, materiali, eventi, videolezioni, materie, flashcard, storico ripassi, blacklist e impostazioni |
| `logs\studyflow.log` | log dell'applicazione, con rotazione |
| `logs\hosts_guard.log` | log del processo elevato |
| `transcripts\*.txt` | trascrizioni delle videolezioni, in UTF-8 |
| `backups\*.zip` | backup di sicurezza creati automaticamente prima di ogni importazione |
| `models\ggml-*.bin` | modelli whisper.cpp scaricati (base o small quantizzato) |
| `hosts_original.backup` | backup una tantum della porzione originale del file hosts |
| `hosts_guard_control.json` | richieste dell'app al guardiano |
| `hosts_guard_status.json` | stato scritto dal guardiano |

La cartella audio `sounds` e' separata: si trova accanto all'applicazione, come
descritto nella sezione Audio, e contiene brani importati, MP3 scaricati da
YouTube e suoni di concentrazione generati. Le videolezioni non vengono copiate:
il database conserva il percorso originale scelto dall'utente.

Il database usa WAL, `foreign_keys=ON`, connessioni thread-locali e timeout per
evitare errori di lock. Il file hosts non viene mai sostituito in blocco: vengono
gestiti solo i marcatori StudyFlow, preservando il contenuto dell'utente.

Per una copia di sicurezza manuale e' sufficiente chiudere l'app e copiare la
cartella `%LOCALAPPDATA%\StudyFlow` (inclusi `transcripts` e `models`, se
presenti) e, se presente, la cartella `sounds` accanto all'applicazione. Per un
backup portabile usare invece **Impostazioni → Esporta dati**: il file ZIP
contiene database, impostazioni, flashcard e trascrizioni ed e' importabile su un
altro PC con la stessa versione di StudyFlow.

---

## 6. Come funziona il blocco dei siti e l'UAC

L'eseguibile principale ha manifest **asInvoker** e non viene avviato come
amministratore. Il flusso e' il seguente:

1. L'utente avvia un Pomodoro di lavoro con il blocco siti abilitato.
2. L'app scrive una richiesta in `hosts_guard_control.json`.
3. Se il guardiano non e' gia' attivo, viene avviato un processo separato tramite
   `ShellExecuteW(..., "runas", ...)`: **solo in questo momento compare il prompt
   UAC**.
4. Il guardiano elevato applica la sezione gestita al file hosts:
   ```text
   # >>> STUDYFLOW MANAGED BLOCK - DO NOT EDIT >>>
   ...
   127.0.0.1 youtube.com
   127.0.0.1 www.youtube.com
   ...
   # <<< STUDYFLOW MANAGED BLOCK <<<
   ```
5. L'app avvia il listener loopback per registrare i tentativi HTTP/HTTPS.
6. Alla fine della fase di lavoro, al reset o alla chiusura, l'app chiede al
   guardiano di rimuovere la sezione e il guardiano ripristina il file hosts.
7. Se l'app termina in modo anomalo, il guardiano rileva la morte del processo
   padre o un heartbeat obsoleto (oltre 150 secondi) e ripristina comunque il
   file hosts.

Il guardiano resta in esecuzione anche tra una sessione e l'altra, cosi' non
viene mostrato un nuovo prompt UAC a ogni Pomodoro. Termina quando l'app si
chiude o quando viene richiesto esplicitamente.

Se l'utente annulla il prompt UAC, il timer continua ma i siti non vengono
bloccati. La vista **Blocco distrazioni** mostra l'errore e il pulsante
**Riprova autorizzazione siti**.

La sezione **Blocco distrazioni > Ripristina file hosts** richiede l'elevazione
solo se esiste davvero una sezione StudyFlow residua o un guardiano attivo.

> Non avviare StudyFlow.exe con "Esegui come amministratore": non e' necessario e
> rende piu' difficile distinguere i privilegi del processo principale da quelli
> del guardiano.

---

## 7. Blocco delle applicazioni

Il monitor dei processi:

- usa `psutil.process_iter` con intervallo configurabile (default 2 secondi);
- confronta il nome del processo senza `.exe` e senza distinzione tra maiuscole
  e minuscole;
- registra il tentativo nel database;
- se l'opzione e' attiva, prova a terminare il processo.

I processi di sistema fondamentali e StudyFlow stesso sono esclusi dalla
terminazione. Applicazioni avviate con privilegi superiori potrebbero non essere
terminabili da una sessione utente standard: in quel caso il tentativo viene
comunque registrato quando rilevato.

---

## 8. Calendario e importazione ICS

Esportare il calendario da Google Calendar nel formato iCalendar (`.ics`) e usare
**Calendario > Importa .ics**.

L'importatore:

- legge file fino a 20 MB;
- importa eventi passati negli ultimi 180 giorni e futuri nei prossimi 365
  giorni;
- espande le ricorrenze con `dateutil.rrule`, gestendo `EXDATE` e `RDATE`;
- converte i fusi orari nel fuso locale di Windows;
- usa le categorie come materia, oppure il titolo se le categorie mancano;
- limita a 500 occorrenze per singolo evento;
- deduplica per `(UID, inizio)` e aggiorna gli eventi gia' presenti.

Gli eventi possono essere aperti nella vista giornaliera o settimanale e avviati
come sessione Pomodoro con **Studia ora**.

### Google Calendar tramite indirizzo iCal (consigliato, senza API)

> Lo stesso tutorial, passo per passo per entrambi i metodi, e' disponibile
> nell'app con il pulsante **? Come Connettere Calendario** (nella vista
> Calendario e nelle Impostazioni).

E' il modo piu' semplice: nessun progetto Google Cloud, nessun login. Per ogni
calendario Google (per esempio uno per materia) si copia il suo **indirizzo
segreto in formato iCal** e lo si incolla in StudyFlow. Gli eventi vengono
scaricati all'avvio e con **Calendario > Sincronizza Ora**.

**Come trovare l'indirizzo segreto iCal di un calendario Google**

1. Apri [Google Calendar](https://calendar.google.com) **dal computer** (l'app
   per telefono non mostra questa impostazione) con l'account che possiede il
   calendario.
2. Nella colonna di sinistra, sotto **I miei calendari**, passa il mouse sul
   calendario (es. "Sistemi Operativi") e clicca sui **tre puntini** ⋮.
3. Scegli **Impostazioni e condivisione**.
4. Scorri fino alla sezione **Integra calendario**.
5. In **Indirizzo segreto in formato iCal** clicca sull'icona dell'occhio per
   mostrarlo, poi sull'icona **Copia**. L'indirizzo ha questa forma:
   `https://calendar.google.com/calendar/ical/<id-calendario>/private-<codice>/basic.ics`
6. In StudyFlow apri **Impostazioni > Calendari tramite indirizzo iCal**,
   incolla l'indirizzo, scegli la materia (facoltativo) e premi **Aggiungi**.
7. Ripeti per ogni calendario. Clicca sul pallino colorato di un calendario
   per sceglierne il colore (l'indirizzo iCal non contiene i colori di Google).

Note:

- **Non usare** l'"Indirizzo pubblico in formato iCal": funziona solo se il
  calendario e' pubblico, cioe' visibile a chiunque.
- L'indirizzo segreto e' come una password: chi lo conosce vede tutti gli
  eventi. StudyFlow lo cifra con Windows (DPAPI), non lo scrive nei log e non
  lo include nel backup ZIP (dopo un ripristino su un altro PC va reincollato).
  Se pensi che sia stato diffuso, nella stessa sezione di Google Calendar usa
  **Reimposta**: il vecchio indirizzo smette di funzionare e in StudyFlow
  comparira' un errore finche' non aggiungi quello nuovo.
- Per un calendario condiviso con te da un'altra persona l'indirizzo segreto
  potrebbe non essere disponibile: puo' copiarlo e mandartelo il proprietario.
- Va bene anche qualsiasi altro indirizzo `.ics`/`webcal://` (Outlook, orari
  universitari...).

Comportamento:

- **Sola lettura**: gli eventi letti da iCal non si modificano in StudyFlow;
  quelli creati con **+ Nuovo Evento** restano solo in StudyFlow.
- **Materia e colore**: un calendario abbinato a una materia assegna quella
  materia e il suo colore a tutti gli eventi; senza materia vale il fallback
  dell'import `.ics` (categorie, poi titolo dell'evento).
- **Cancellazione speculare**: un evento eliminato su Google sparisce da
  StudyFlow alla sincronizzazione successiva; sessioni e materiali collegati
  restano (viene solo azzerato il collegamento all'evento). Rimuovendo un
  indirizzo dalle Impostazioni spariscono i suoi eventi.
- Google aggiorna il file iCal con un certo ritardo: una modifica appena fatta
  su Google puo' comparire in StudyFlow solo dopo un po'.

### Google Calendar tramite API (avanzato, sincronizzazione bidirezionale)

**Configurazione (una sola volta)**

1. In [Google Cloud Console](https://console.cloud.google.com/) crea un progetto,
   abilita la **Google Calendar API** e configura la schermata di consenso OAuth
   (tipo "Esterno", aggiungendo il tuo account tra gli utenti di test).
2. In **Credenziali > Crea credenziali > ID client OAuth** scegli il tipo
   **App desktop** e scarica il file JSON.
3. In StudyFlow apri **Impostazioni > Google Calendar**, premi **Carica
   credentials.json**, poi **Connetti account** e accetta nel browser.

Il consenso usa OAuth 2.0 per app desktop: PKCE (S256) e un server di ritorno su
`127.0.0.1` con porta effimera. `access_token`, `refresh_token` e
`client_secret` sono cifrati con Windows DPAPI (legati all'utente Windows), non
compaiono mai in `studyflow.log` e sono esclusi dal backup ZIP; un ripristino
conserva il collegamento Google gia' presente sul PC.

**Calendari, materie e colori**

- I calendari con lo stesso nome di una materia (maiuscole ignorate) vengono
  abbinati e abilitati automaticamente; gli altri si abbinano dalle
  Impostazioni. Un calendario senza materia usa il titolo dell'evento come
  materia (stesso comportamento dell'import `.ics`).
- Il colore del calendario diventa il colore della materia (`subjects.color_hex`);
  un `colorId` sul singolo evento ha la precedenza. L'agenda usa questi colori
  con contrasto del testo automatico; la pagina della materia mostra il badge
  del calendario collegato.
- Opzione facoltativa: **Crea un calendario Google per ogni nuova materia**.
- Finestra di sincronizzazione configurabile (predefinita: 180 giorni passati,
  365 futuri).

**Come funziona la sincronizzazione**

1. *Flush della coda* (StudyFlow -> Google): `google_pending_create` (POST),
   `google_pending_update` (`move` se la materia ha cambiato calendario, poi
   PATCH), `google_pending_delete` (DELETE; il record "tombstone" sparisce su
   204/410). La coda viene inviata subito dopo ogni modifica, in background; se
   sei offline resta in attesa della sincronizzazione successiva.
2. *Pull* (Google -> StudyFlow) per ogni calendario abilitato, con upsert su
   `(google_calendar_id, google_event_id)`. Le modifiche locali ancora in coda
   non vengono mai sovrascritte.
3. *Cancellazione speculare*: un evento eliminato su Google sparisce anche da
   StudyFlow; sessioni e materiali collegati restano (viene solo azzerato
   `calendar_event_id`).

Gli eventi importati da `.ics` restano locali; un nuovo evento con una materia
senza calendario Google resta solo in StudyFlow (`source = 'manual'`).

---

## 9. Materie, materiali e ore di studio

### Pagina materia

La vista **Materie** raccoglie tutte le materie dell'applicazione. Una materia
puo' essere creata manualmente oppure comparire automaticamente perche' usata in
un materiale, in una videolezione, in una flashcard, in un evento del calendario
o in una sessione Pomodoro. Per ogni materia e' disponibile una pagina con:

- nome della materia;
- nome e email del docente (facoltativi);
- numero di CFU e monte ore calcolato;
- data dell'esame, oppure l'indicazione **non ancora definita**;
- note libere;
- riepilogo dei collegamenti e delle ore gia' registrate.

Il monte ore usa il rapporto previsto dalla normativa universitaria italiana:

```text
1 CFU = 25 ore di impegno complessivo
```

Il conteggio comprende lezioni, esercitazioni e studio individuale. Per esempio
9 CFU corrispondono a 225 ore. La vista mostra anche la percentuale di ore
registrate rispetto al monte ore, ma non impone un obiettivo: le ore previste
restano un riferimento informativo.

Se la data d'esame e' definita, la pagina mostra un countdown aggiornato ogni
minuto fino alla mezzanotte del giorno dell'esame. Durante la giornata compare
**Esame oggi**; dopo la data viene indicato da quanti giorni e' passata. Se la
data non e' ancora nota, la materia resta comunque salvabile e modificabile in
qualsiasi momento.

La rinomina di una materia aggiorna automaticamente il campo `subject` in
sessioni, materiali, videolezioni, flashcard ed eventi del calendario. Per
proteggere i dati, l'eliminazione dei dettagli e' consentita solo se la materia
non ha elementi collegati.

### Materiali

I materiali sono gestiti all'interno della pagina della materia: si aprono dalla
sezione **MATERIALI**, subito sotto il countdown dell'esame. Non esiste piu' una
voce **Materiali** nella barra di navigazione.

Un materiale contiene:

- titolo, materia e percorso del file;
- ore previste;
- evento del calendario opzionale;
- ore registrate.

Il materiale viene creato e modificato restando collegato alla materia della
pagina. Se in un backup o in dati precedenti esistono materiali senza materia,
compare temporaneamente la voce **Materiali senza materia**: aprendo **Modifica**
si puo' assegnare loro una materia esistente o scriverne una nuova.

Il file originale non viene copiato ne' modificato. Se viene spostato o
cancellato, StudyFlow mostra "non trovato" e permette comunque di modificare il
record.

Avviando un Pomodoro da un materiale, i minuti effettivi vengono aggiunti alle
ore registrate sia che la sessione venga completata sia che venga interrotta.

### Cartella di ogni materia

Ogni materia ha una cartella su disco nella radice di funzionamento dell'app
(accanto a `StudyFlow.exe`, oppure nella radice del progetto in modalita'
sorgente; se non e' scrivibile si usa `%LOCALAPPDATA%\StudyFlow\Materie`.
`STUDYFLOW_SUBJECTS_DIR` permette di scegliere un altro percorso):

```text
Materie/
  <Nome materia>/
    Trascrizioni/   .txt delle videolezioni della materia
    Appunti/        export TXT/LaTeX/Word di PDF e Slide
    Flashcard/      Flashcard.csv (Fronte;Retro;Note), aggiornato a ogni modifica
```

- La cartella viene creata quando la materia nasce (anche se scritta per la
  prima volta in un video, una flashcard o un materiale) e all'avvio per tutte
  le materie note.
- Rinominando la materia la cartella viene rinominata e i percorsi salvati
  (materiali, trascrizioni) vengono aggiornati; eliminando la materia la
  cartella viene rimossa solo se vuota.
- **Video e Trascrizione**: la trascrizione viene scritta nella cartella
  `Trascrizioni` della materia scelta; cambiando materia a una videolezione il
  .txt viene spostato.
- **PDF e Slide**: nella card *Esportazione* si sceglie la **Materia**; gli
  export vengono proposti in `Appunti` e registrati come materiali della
  materia.
- La pagina materia mostra, oltre ai **MATERIALI**, le sezioni
  **VIDEOLEZIONI E TRASCRIZIONI** e **FLASHCARD**, con i pulsanti per aprire i
  file, la cartella o la sezione corrispondente (es. *Ripassa*).

I file originali scelti dall'utente (PDF, video, dispense) non vengono copiati.

---

## 10. Audio, YouTube e suoni di concentrazione

### Libreria locale
I file audio si trovano nella sottocartella `sounds` della cartella
dell'applicazione:

```text
D:\StudyApp\sounds\                 # avvio da sorgente
D:\StudyApp\dist\sounds\            # exe nella cartella dist
```

Se l'applicazione viene installata in una posizione non scrivibile, StudyFlow usa
automaticamente `%LOCALAPPDATA%\StudyFlow\sounds\`. Con `--data-dir` o
`STUDYFLOW_DATA_DIR` la cartella diventa `<data-dir>\sounds`; si puo' forzare un
percorso con `STUDYFLOW_SOUNDS_DIR`.

Sono supportati MP3, OGG, WAV e FLAC. La vista **Audio** permette di
riprodurli, importarli dal disco e aprire la cartella. La riproduzione usa
`pygame.mixer`, importato solo al primo utilizzo.

### Download da YouTube
YouTube non espone una modalita' di ascolto ufficiale solo-audio: il browser
riproduce il flusso video e non esiste un'API pubblica per ottenere soltanto la
traccia audio. StudyFlow quindi scarica la traccia migliore con `yt-dlp`, la
converte in MP3 a 192 kbps con FFmpeg e la salva con il titolo del video.

Procedura:

1. Incollare un link `youtube.com`, `youtu.be` o Shorts nella vista **Audio**.
2. Premere **Scarica audio**. StudyFlow legge prima titolo e durata.
3. Se esiste gia' un file con lo stesso nome, scegliere:
   - **Crea Copia**: salva come `Titolo (copia 2).mp3`;
   - **Vai a Cartella**: apre Esplora file con il file esistente selezionato;
   - **Salta**: annulla il download.
4. Il file viene salvato in `sounds` e selezionato come brano predefinito.
   I caratteri non validi per Windows vengono sostituiti e i nomi riservati
   (`CON`, `PRN`, ecc.) sono prefissati con `_`.

FFmpeg deve essere disponibile nel `PATH`, accanto a `StudyFlow.exe`, nella
cartella che contiene `sounds`, oppure in `%LOCALAPPDATA%\StudyFlow\ffmpeg.exe` /
`%LOCALAPPDATA%\StudyFlow\ffmpeg\ffmpeg.exe`. La vista Audio mostra lo stato del
rilevamento.

Per risolvere le sfide JavaScript di YouTube, StudyFlow include `yt-dlp-ejs` e
rileva automaticamente Deno, Node.js 22+, Bun o QuickJS nel `PATH`. Node.js 22+
oppure Deno sono consigliati per la massima compatibilita'.

> Scaricare audio e' consentito solo per contenuti di cui si possiedono i diritti
> o che la licenza permette di scaricare. Rispettare i termini di servizio di
> YouTube e le norme sul diritto d'autore.

### Musica e Pomodoro
- **Brano predefinito**: il file usato dall'avvio automatico.
- **Avvia con il Pomodoro**: parte all'inizio di ogni fase di lavoro.
- **Ripeti in loop**: continua fino alla fine della sessione.
- Il volume e' regolabile dal lettore e viene salvato.
- La musica automatica va in pausa quando il timer viene messo in pausa e si
  ferma alla fine della fase di lavoro.
- Se si riproduce un brano manualmente, StudyFlow non lo interrompe al cambio
  fase.

### Suoni generati
Il pulsante **Genera suoni mancanti** crea nella cartella `sounds` quattro loop
WAV mono di 30 secondi: rumore bianco, rumore marrone, pioggia leggera e onde
morbide. Sono generati localmente, non richiedono rete e non hanno licenze
esterne.

---

## 11. Video e Trascrizione

La vista **Video e Trascrizione** mantiene un archivio locale di videolezioni
collegate a una materia o a un tag del calendario. Ogni record contiene titolo,
percorso del video, materia, percorso e nome del file `.txt`, stato, progresso,
modello, lingua e date di creazione/aggiornamento.

Il video non viene copiato ne' modificato: StudyFlow conserva il percorso
originale. Se il file viene spostato o cancellato, la scheda lo segnala e permette
di modificarne i metadati o rimuovere il record senza toccare il disco.

### Flusso di utilizzo

1. Aprire **Video e Trascrizione** e premere **Aggiungi video**.
2. Scegliere il file (MP4, MKV, MOV, AVI, WEBM e altri formati comuni), inserire
   un titolo e una materia/tag. Il nome predefinito del `.txt` e' il nome del video
   senza estensione.
3. Selezionare la videolezione e premere **Trascrivi**. Nel dialogo si possono
   cambiare nome del `.txt`, materia, modello e lingua.
4. La trascrizione parte in background: la UI resta utilizzabile e mostra fase,
   percentuale e tempo audio elaborato.
5. Al termine il `.txt` compare nel viewer integrato. Da qui si puo' aprire il
   file, copiare il testo o rinominarlo.

### Estrazione audio e motore

- FFmpeg estrae la traccia audio in un WAV **mono 16 kHz** temporaneo, il formato
  richiesto da `whisper.cpp`. Il WAV viene eliminato al termine o in caso di
  errore.
- La trascrizione usa **`pywhispercpp` 1.5.1**, il binding Python leggero per
  `whisper.cpp`, ed esegue sulla CPU.
- Modelli disponibili:
  - `base-q5_1`, predefinito, circa **60 MB**, buon compromesso per lezioni
    italiane;
  - `small-q5_1`, opzionale, circa **190 MB**, piu' preciso ma piu' lento.
- I modelli sono quantizzati (`q5_1`) e vengono scaricati una sola volta da
  Hugging Face nella cartella `models` dei dati. Dopo il primo download la
  trascrizione funziona completamente offline.
- La lingua predefinita e' l'italiano; sono disponibili anche inglese e
  rilevamento automatico.

### Esecuzione asincrona e avanzamento

La trascrizione gira in un processo separato (`multiprocessing`, contesto
`spawn`), non in un thread della UI. In questo modo il binding nativo non blocca
Tkinter e la finestra resta reattiva anche durante l'elaborazione.

La barra di avanzamento e' calcolata sul rapporto tra durata audio gia'
processata e durata totale. Le fasi mostrate sono:

```text
Preparazione modello -> Estrazione audio -> Trascrizione
```

Al termine della trascrizione il `.txt` viene scritto in modo atomico; solo dopo
il record passa a `completed`.

Se il modello non e' ancora presente, il download mostra una percentuale
separata. Una sola trascrizione puo' essere attiva alla volta; le altre schede
mostrano un avviso.

### File `.txt`

- Nome predefinito: `{nome_video}.txt`, modificabile prima di avviare e dopo il
  completamento con **Rinomina .txt**.
- Codifica UTF-8, con intestazione che riporta titolo, video, materia, durata,
  modello, lingua e data.
- Ogni blocco di parlato inizia con un timestamp `[mm:ss]`, ad esempio:

```text
[00:00] Questa e' una lezione di prova sulla fotosintesi.
[00:04] Le piante trasformano la luce del sole in energia chimica.
```

- Il viewer integrato legge il file senza aprire programmi esterni. Sono
  disponibili anche **Apri .txt** e **Copia testo**.
- Se si ripete la trascrizione, il file precedente viene sostituito solo al
  termine di quella nuova. Un annullamento o un errore non lo cancella.

### Archivio, filtri e stato

- Filtro per materia/tag e ricerca testuale su titolo e percorso.
- Riepilogo compatto con numero di videolezioni, trascrizioni e materie.
- Stati: `ready` (da trascrivere), `processing`, `completed`, `cancelled`,
  `error`.
- I tag materia sono condivisi con calendario, materiali e sessioni, cosi' lo
  stesso corso resta coerente in tutte le viste.
- Se l'app viene chiusa durante una trascrizione, il processo viene annullato e
  il record viene marcato come `cancelled`; un `.txt` precedente resta intatto.
- Se l'app termina in modo imprevisto, all'avvio successivo i record rimasti in
  `queued`/`processing` vengono riportati a `error` e possono essere riavviati.

### Percorsi

```text
%LOCALAPPDATA%\StudyFlow\transcripts\   # file .txt
%LOCALAPPDATA%\StudyFlow\models\        # ggml-*.bin
```

Con `--data-dir` o `STUDYFLOW_DATA_DIR` entrambe le cartelle si trovano sotto la
cartella dati scelta.

---

## 11b. PDF e Slide

La vista **PDF e Slide** converte slide e dispense in testo modificabile. Tutto
il lavoro avviene sul PC: nessun file viene inviato a servizi esterni.

### Flusso di utilizzo

1. **Apri PDF…** (uno o piu' file) oppure **Apri cartella…** per il batch.
2. **Estrai testo**: la barra mostra pagina e file in elaborazione; **Annulla**
   interrompe in sicurezza.
3. Il testo compare nell'editor a sinistra, l'anteprima a destra si aggiorna a
   ogni modifica. Con il selettore **Anteprima / LaTeX / TXT** si vede il
   risultato finale di ciascun formato; **Affiancati / Sovrapposti** cambia la
   disposizione.
4. **Copia LaTeX**, **.txt / .tex / .docx** per il documento aperto, oppure
   **Esporta tutti…** per salvare l'intero batch in una cartella.

### Motori

| Caso | Motore | Note |
|---|---|---|
| PDF vettoriali (PowerPoint, Word, LaTeX, Beamer) | PyMuPDF | predefinito, veloce |
| Alternativa | pdfplumber | usato se PyMuPDF manca o scelto a mano |
| Pagine scansionate | Tesseract (TSV) o PaddleOCR | in automatico se una pagina ha immagini ma non testo |
| Formule complesse | Marker o Nougat | solo pagine con formule oppure tutte |

Tesseract viene cercato nel `PATH`, in `C:\Program Files\Tesseract-OCR`, nella
cartella dati (`tesseract\tesseract.exe`) o nella variabile `STUDYFLOW_TESSERACT`.
Le lingue (`ita+eng`) vengono ridotte a quelle installate.

Marker e Nougat sono CLI basate su PyTorch, quindi non sono incluse nell'exe: si
installano a parte (anche in un altro ambiente Python) e si indica il comando
nella scheda **Motori**. I segnaposto sono `{input_dir}`, `{output_dir}` e
`{inputs}`; i predefiniti sono:

```text
marker {input_dir} --output_dir {output_dir}
nougat {inputs} -o {output_dir} --no-skipping
```

Ogni pagina selezionata viene salvata come PDF singolo e il motore viene avviato
una sola volta su tutti i file, cosi' il modello si carica una volta e ogni output
resta associato alla sua slide. Se il motore fallisce, StudyFlow usa il
riconoscimento integrato e lo segnala sotto l'editor. Al primo avvio Marker e
Nougat scaricano i propri modelli: e' un comportamento loro, attivato solo se
scelti esplicitamente.

### Riconoscimento delle formule integrato

- PDF da LaTeX/Beamer: i glifi dei font matematici (Computer Modern, AMS, CMEX)
  vengono ricomposti in base alla posizione: apici e pedici dalla dimensione e
  dallo spostamento verticale, frazioni da numeratore e denominatore sovrapposti,
  limiti di `\int` e `\sum` dai glifi sopra/sotto il simbolo, radice fino al
  primo simbolo di relazione, `\mathbb`, `\mathcal`, `\vec`, nomi di funzione
  (`\lim`, `\sin`...). I frammenti che PyMuPDF separa su righe diverse vengono
  riuniti alla riga principale.
- PDF da PowerPoint/Word: i simboli Unicode (`α`, `≤`, `x²`, `½`) diventano
  LaTeX quando formano una formula; frecce e puntini nella prosa restano testo.
- Una riga composta solo da una formula diventa `$$...$$`; i numeri di
  equazione `(1)` restano accanto come `\qquad (1)`.

Il riconoscimento euristico copre bene le formule tipiche delle slide; per
matrici, sistemi e allineamenti complessi conviene Marker o Nougat, oppure una
correzione rapida nell'editor.

### Formato dell'editor

```text
=== SLIDE 1 ===

# Titolo della slide

Testo con formula inline $E = mc^{2}$ e un dollaro letterale \$.

- punto elenco
  - punto annidato
1. elenco numerato

$$
\int_{0}^{1} x^{2}\,dx = \frac{1}{3}
$$
```

Con **Modifica in: LaTeX** l'editor contiene direttamente il corpo LaTeX; il
ritorno a Markdown riconverte i comandi noti e lascia gli altri come testo.

### Esportazione

- **.txt**: testo pulito diviso con `=== SLIDE N ===`; le formule semplici
  diventano leggibili (`\alpha^{2}` -> `α²`).
- **.tex**: corpo (`% === Slide N ===`, `\section*`, `itemize`, `enumerate`,
  `\[ ... \]`) oppure documento completo `article` con `amsmath`, `amssymb`,
  `babel` italiano. Le formule display usano `\[ ... \]`, la forma raccomandata
  da amsmath ed equivalente a `$$ ... $$`.
- **.docx**: Pandoc locale; `$...$` e `$$...$$` diventano equazioni native
  di Word, titoli con gli stili Titolo 1/2, elenchi Word, marcatori di slide con
  lo stile "Slide Marker". Opzione per una nuova pagina a ogni slide. Per usare
  stili personalizzati basta copiare un modello Word in
  `%LOCALAPPDATA%\StudyFlow\pdf_reference.docx`.
- Le opzioni **Marcatori e numeri di slide**, **Titoli delle slide** e
  **Preambolo LaTeX** valgono per anteprima, copia ed esportazione.

### Rimuovi testo

Il pulsante **✂ Rimuovi testo** (o **Ctrl+H** nell'editor) apre una finestra
che elimina tutte le occorrenze di un testo ridondante (nome del corso, docente,
copyright, "Pag. N"...) dal sorgente, quindi da anteprima, copia ed export.

- Se nell'editor c'e' una selezione, diventa il testo da cercare.
- Le occorrenze vengono contate ed evidenziate nell'editor mentre scrivi.
- Opzioni: maiuscole/minuscole, parola intera, espressione regolare (es.
  `^Pag\. \d+$`), spazi e a capo flessibili (uno spazio cercato corrisponde
  anche a un a capo, utile quando il PDF spezza le righe in punti diversi).
- **Elimina righe, punti elenco e titoli rimasti vuoti**: pulisce doppi spazi,
  separatori orfani (`Titolo - Prof. X` -> `Titolo`) e righe svuotate.
- Ambito: documento aperto o tutti i documenti estratti.
- I marcatori `=== SLIDE N ===` non vengono mai toccati.
- **Annulla rimozione** ripristina tutti i documenti modificati; sul documento
  aperto funziona anche Ctrl+Z.
- **Ricorda e applica alle prossime estrazioni** salva la regola: con l'opzione
  in *Pulizia e struttura* attiva viene applicata in automatico a ogni nuovo PDF.

### Anteprima

L'anteprima interna disegna le formule con `matplotlib.mathtext` (offline, gia'
tra le dipendenze); le formule non supportate da mathtext (ambienti
`\begin{...}`, righe multiple) appaiono come codice evidenziato. **Anteprima
KaTeX** apre nel browser una pagina HTML che usa il bundle KaTeX incluso in
`assets/katex`, senza connessione.

---

## 12. Flashcard e ripetizione spaziata

La vista **Flashcard** mantiene un archivio di schede collegate alle materie gia'
presenti in calendario, materiali, sessioni e videolezioni. Ogni scheda ha un
fronte (domanda), un retro (risposta), note facoltative e uno stato di
pianificazione.

### Stati e giudizi

| Stato | Significato |
|---|---|
| `new` | scheda mai studiata |
| `learning` | scheda nei passi di apprendimento |
| `review` | scheda in ripasso a intervalli crescenti |
| `relearning` | scheda ricaduta dopo un lapse |

I giudizi disponibili sono quattro, con le scorciatoie `1`-`4` durante la
sessione:

| Tasto | Giudizio | Comportamento |
|---|---|---|
| `1` | Ancora | riparte dal primo passo, riduce la facilita' e registra un lapse |
| `2` | Difficile | resta nel passo corrente o accorcia l'intervallo |
| `3` | Bene | avanza di passo o moltiplica l'intervallo per la facilita' |
| `4` | Facile | laurea subito la scheda o allunga maggiormente l'intervallo |

`Spazio` mostra la risposta, `Esc` chiude la sessione. Le anteprime sui pulsanti
mostrano l'intervallo che verrebbe applicato prima di rispondere.

### Algoritmo

Il motore in `study_manager/services/spaced_repetition.py` applica i passi di
apprendimento in stile Anki sopra la curva SM-2 di SuperMemo:

```text
passi di apprendimento      1 min, 10 min
intervallo di laurea        1 giorno
intervallo "Facile"         4 giorni
passi di riapprendimento    10 min
fattore di facilita'        2.50 iniziale, limitato tra 1.30 e 5.00
intervallo massimo          100 anni
lapse                       facilita' -0.20, intervallo ridotto al 20%
```

In ripasso l'intervallo viene arrotondato a giorni interi e moltiplicato per il
fattore di facilita'. `Difficile` usa un moltiplicatore ridotto (1.2) e abbassa la
facilita', `Facile` aggiunge un ulteriore 30% e la alza. Dopo un lapse la scheda
torna in riapprendimento e deve essere riconfermata prima di rientrare in ripasso.

Le schede e lo storico sono salvati nelle tabelle `flashcards` e
`flashcard_reviews` (schema SQLite versione 5). Lo storico registra giudizio,
data, stato precedente e nuovo, intervalli, facilita' e tempo trascorso, cosi' le
statistiche restano verificabili.

### Filtri e statistiche

- Materia, stato e ricerca testuale si combinano tra loro.
- **Da ripassare** mostra solo le schede con scadenza raggiunta, incluse le nuove.
- I contatori in alto riepilogano scadenze, schede nuove, schede in apprendimento,
  totale e ripassi completati nella giornata.
- **Ripristina** riporta una scheda allo stato nuovo e cancella il suo storico di
  pianificazione, dopo conferma.

---

## 13. Backup, esportazione e importazione

Nella vista **Impostazioni** sono disponibili i pulsanti **Esporta dati** e
**Importa dati**.

### Formato dell'archivio

Il backup e' un file ZIP portabile con questa struttura:

```text
StudyFlow-backup-YYYYmmdd-HHMMSS.zip
├── manifest.json        # formato, versione, data e conteggi
├── data.json            # tutte le tabelle applicative
└── transcripts/
    └── *.txt            # trascrizioni delle videolezioni
```

`manifest.json` contiene `format: "studyflow-backup"`, `backup_version`,
`schema_version`, `app_version`, la data di esportazione e il numero di righe per
tabella. `data.json` contiene le tabelle `settings`, `subjects`,
`calendar_events`, `sessions`, `materials`, `distraction_attempts`,
`video_lessons`, `flashcards`, `flashcard_reviews`, `blocked_sites` e
`blocked_apps`.

Non vengono inclusi:

- i video originali e i PDF, che restano nei percorsi scelti dall'utente;
- i file audio della cartella `sounds`;
- i modelli whisper.cpp in `models`;
- i log e i file di stato del guardiano hosts.

### Importazione

L'importazione e' **sostitutiva**: dopo la conferma, tutte le tabelle applicative
vengono svuotate e ricaricate dal backup, poi le trascrizioni vengono estratte
nella cartella `transcripts`. Prima di modificare qualsiasi dato, StudyFlow crea
automaticamente un backup di sicurezza:

```text
%LOCALAPPDATA%\StudyFlow\backups\backup-pre-import-YYYYmmdd-HHMMSS.zip
```

Se un percorso di trascrizione salvato nel backup non esiste su quel PC, l'app
prova a ritrovare il file con lo stesso nome nella cartella `transcripts` corrente
e aggiorna il record. L'importazione viene bloccata se il Pomodoro e' in corso o
se e' attiva una trascrizione, per evitare stati incoerenti. Un archivio con
formato non riconosciuto, versione piu' recente o riferimenti non validi viene
rifiutato senza toccare i dati esistenti.

Al termine l'app ricarica impostazioni, blocker, Pomodoro e tutte le viste gia'
aperte, senza richiedere un riavvio.

---

## 14. Quality Score

Il punteggio e' compreso tra 0 e 100 ed e' calcolato sulle sessioni del mese
corrente:

```text
35%  completamento dei Pomodori avviati
40%  Pomodori completati senza alcuna distrazione
25%  attenzione, penalizzata dalle interruzioni per ora di studio
```

Formula applicata:

```text
completion_rate = completati / avviati
clean_rate      = pomodori_puliti / completati
attention       = max(0, 100 - (interruzioni / ore) * 9)
score           = completion_rate*35 + clean_rate*40 + attention/100*25
```

Se non ci sono sessioni nel periodo il punteggio e' 0.

---

## 15. Test

I test non aprono la GUI e non modificano il file hosts reale:

```powershell
py -3.12 -m unittest discover -s tests -v
```

Copertura attuale:

- schema, seed, impostazioni, sessioni, tentativi, materiali, calendario;
- apply/restore della sezione hosts su file temporaneo;
- nessuna richiesta UAC quando non esiste una sezione hosts residua;
- limiti del Quality Score;
- transizioni del motore Pomodoro;
- persistenza della sessione e aggiornamento delle ore materiale;
- validazione dei link YouTube, nomi file Windows-safe e gestione copie;
- generazione dei suoni WAV e controlli del mixer con driver audio dummy;
- timestamp `[mm:ss]`, intestazione e caso senza parlato delle trascrizioni;
- comando FFmpeg mono 16 kHz, durata WAV, rinomina senza sovrascritture e
  percorsi dei modelli;
- ciclo di vita delle videolezioni nel database, filtro materia e recupero dei
  job interrotti;
- conversione CFU/ore, formati della data d'esame e stati del countdown;
- creazione, aggiornamento, rinomina propagata e protezione dall'eliminazione
  delle materie;
- passi di apprendimento, intervalli SM-2, lapse, limiti del fattore di facilita'
  e formattazione degli intervalli delle flashcard;
- ciclo di vita delle flashcard, filtri, ricerca, statistiche e storico ripassi;
- esportazione ZIP, validazione degli archivi, importazione sostitutiva e backup
  di sicurezza.

La suite attuale contiene **55 test**.

Smoke test automatico (usa una cartella dati temporanea e non richiede UAC):

```powershell
py -3.12 main.py --smoke-test
```

Lo smoke test apre tutte le viste, carica i grafici, verifica i backend audio e
trascrizione, avvia e azzera un timer e chiude l'applicazione. L'equivalente sulla
build e':

```powershell
Start-Process .\dist\StudyFlow.exe -ArgumentList '--smoke-test' -Wait -PassThru
```

Il codice di uscita deve essere `0` e il log deve contenere
`Smoke test completato correttamente.`.

Per verificare anche una trascrizione reale nella build, indicare un video e una
cartella dati persistente. Se il modello manca, viene scaricato durante il test:

```powershell
$env:STUDYFLOW_SMOKE_TRANSCRIBE_VIDEO = "C:\video\lezione.mp4"
Start-Process .\dist\StudyFlow.exe `
  -ArgumentList '--smoke-test','--data-dir','C:\StudyFlowSmoke' `
  -Wait -PassThru
Remove-Item Env:STUDYFLOW_SMOKE_TRANSCRIBE_VIDEO
```

Per evitare il download, copiare il modello in
`C:\StudyFlowSmoke\models\ggml-base-q5_1.bin`. La verifica richiede FFmpeg e puo'
durare piu' a lungo; il log riporta il percorso del `.txt` prodotto e il numero
di blocchi.

Per verificare anche il download YouTube e la decodifica MP3 nella build, si puo'
impostare un URL pubblico nella variabile `STUDYFLOW_SMOKE_YOUTUBE_URL`. Il test
scarica il brano nella cartella `sounds` temporanea e lo riproduce con il driver
audio dummy:

```powershell
$env:STUDYFLOW_SMOKE_YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
$env:SDL_AUDIODRIVER = "dummy"
Start-Process .\dist\StudyFlow.exe -ArgumentList '--smoke-test' -Wait -PassThru
Remove-Item Env:STUDYFLOW_SMOKE_YOUTUBE_URL
Remove-Item Env:SDL_AUDIODRIVER
```

Questa verifica richiede FFmpeg e una connessione Internet.

---

## 16. Build dell'eseguibile singolo

### Metodo automatico consigliato

```powershell
Set-Location D:\StudyApp
.\build.ps1
```

Lo script:

1. crea `.venv` con Python 3.12 se non esiste;
2. installa le dipendenze da `requirements.txt`;
3. esegue i test (salvo `-SkipTests`);
4. esegue PyInstaller con `StudyFlow.spec`;
5. verifica che `dist\StudyFlow.exe` esista.

Per saltare i test:

```powershell
.\build.ps1 -SkipTests
```

### Comandi PyInstaller esatti

```powershell
Set-Location D:\StudyApp
py -3.12 -m pip install -r requirements.txt
py -3.12 -m PyInstaller --noconfirm --clean StudyFlow.spec
```

Risultato:

```text
D:\StudyApp\dist\StudyFlow.exe
```

Non serve aggiungere `--onefile`: il file `.spec` contiene gia' una build
one-file con:

- icona `assets/app.ico`;
- metadati `version_info.txt`;
- risorse CustomTkinter;
- dati `tzdata`;
- backend Matplotlib TkAgg;
- Pygame e `yt-dlp` con i relativi hook PyInstaller;
- `pywhispercpp` con metadati `dist-info` e DLL native `ggml-*.dll`,
  `whisper-*.dll`, `msvcp140-*.dll`, `vcomp140-*.dll`;
- `console=False`;
- `uac_admin=False`, quindi manifest **asInvoker**.

FFmpeg e i modelli GGML non vengono inclusi nell'eseguibile. FFmpeg aggiungerebbe
circa 80 MB e rallenterebbe l'estrazione one-file a ogni avvio; i modelli (60 MB o
190 MB) vengono scaricati solo quando servono. Per il download YouTube e per la
trascrizione FFmpeg va installato separatamente oppure copiato nella cartella
dati.

Per ricostruire da zero:

```powershell
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
py -3.12 -m PyInstaller --noconfirm --clean StudyFlow.spec
```

### Verifica dell'eseguibile

```powershell
.\dist\StudyFlow.exe
.\dist\StudyFlow.exe --version
```

Poiche' l'exe usa `console=False`, `--version` mostra una finestra di messaggio
con la versione invece di scrivere nella console.

La prima volta che si avvia un Pomodoro con il blocco siti attivo deve comparire
un solo prompt UAC. Dopo l'approvazione, il file hosts deve contenere la sezione
StudyFlow e deve essere ripristinato alla chiusura dell'app.

Per controllare il manifest:

```powershell
Get-Item .\dist\StudyFlow.exe | Select-Object Name, Length, VersionInfo
(Get-Item .\dist\StudyFlow.exe).VersionInfo.FileVersion
```

La versione attesa per questa release e' `1.4.2.0`. Se si distribuisce l'exe a un
altro PC, non serve Python: servono solo `StudyFlow.exe` e i permessi utente
normali. Il guardiano hosts viene avviato dallo stesso exe con l'argomento
interno `--hosts-guard`. Per il download YouTube e per la trascrizione serve
anche FFmpeg su quel PC; tutte le altre funzioni audio locali funzionano senza
dipendenze esterne. Il modello di trascrizione viene scaricato al primo uso, se
manca.

### Aggiornare la versione

Quando si cambia versione, aggiornare in modo coerente:

- `study_manager/__init__.py` (`__version__`);
- `study_manager/config.py` (`APP_VERSION`);
- `version_info.txt` (`filevers`, `prodvers`, `FileVersion`, `ProductVersion`).

---

## 17. Risoluzione dei problemi

### L'app non parte e segnala una dipendenza mancante
Attivare il virtual environment e reinstallare:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Il prompt UAC non compare
Verificare che il blocco siti sia abilitato e che ci siano domini attivi nella
blacklist. Aprire **Blocco distrazioni** e controllare il messaggio di stato.
Se il prompt e' stato annullato, usare **Riprova autorizzazione siti** durante
una sessione attiva.

### I siti non vengono bloccati
- Controllare che il prompt UAC sia stato accettato.
- Verificare che il browser non usi DNS-over-HTTPS/VPN (vedi limiti).
- Controllare che il file hosts contenga i marcatori StudyFlow.
- Provare **Ripristina file hosts** e riavviare la sessione.

### Una sezione StudyFlow resta nel file hosts
Aprire **Blocco distrazioni** e premere **Ripristina file hosts**. Il pulsante
richiede UAC solo se la sezione esiste ancora. In alternativa, aprire il file
`C:\Windows\System32\drivers\etc\hosts` come amministratore e rimuovere solo il
blocco compreso tra i due marcatori.

### I grafici non compaiono
La dashboard carica Matplotlib alla prima apertura. Se la cartella dati o
l'ambiente Python sono danneggiati, controllare `logs\studyflow.log`.

### Il download YouTube segnala FFmpeg mancante
Installare FFmpeg e riavviare StudyFlow:

```powershell
winget install Gyan.FFmpeg
```

In alternativa copiare `ffmpeg.exe` accanto a `StudyFlow.exe`, in
`%LOCALAPPDATA%\StudyFlow\` oppure in una sottocartella `ffmpeg` di una di queste
posizioni. La vista **Audio** mostra `FFmpeg pronto` quando il binario viene
rilevato.

### La trascrizione segnala FFmpeg mancante
La trascrizione usa lo stesso FFmpeg del download YouTube. Installarlo con
`winget install Gyan.FFmpeg` oppure copiarlo nelle posizioni indicate sopra, poi
riavviare StudyFlow. Senza FFmpeg non e' possibile estrarre l'audio, ma archivio,
filtri e viewer dei `.txt` gia' esistenti restano utilizzabili.

### Il download del modello di trascrizione non riesce
Il modello viene scaricato da Hugging Face solo la prima volta. Controllare la
connessione, eventuali proxy/firewall e lo spazio libero (almeno 250 MB per
`small-q5_1`). I download incompleti usano un file `.part` e vengono rimossi
automaticamente; riprovare dal dialogo **Trascrivi**. Se il problema persiste,
eliminare `models\ggml-*.bin.part` dalla cartella dati e ripetere.

### La trascrizione e' lenta
L'elaborazione avviene sulla CPU e richiede tempo proporzionale alla durata del
video. Il modello `base-q5_1` e' il piu' rapido; `small-q5_1` e' piu' preciso ma
piu' lento. Chiudere applicazioni pesanti, usare video con audio chiaro e
lasciare StudyFlow aperto: la trascrizione continua in background e non blocca
la navigazione.

### La trascrizione termina con un errore
Controllare che il video esista ancora nel percorso indicato e che FFmpeg sia
rilevato. Aprire `logs\studyflow.log` per il dettaglio tecnico, quindi premere
**Ripeti trascrizione**. Se l'app e' stata chiusa durante il lavoro, il record
viene marcato `cancelled`; se invece e' terminata in modo imprevisto, compare
come `error`. In entrambi i casi il `.txt` precedente non viene toccato.

### Il file `.txt` non compare nel viewer
Verificare che la trascrizione sia `completed` e che il file esista nella
cartella `transcripts`. Usare **Apri cartella trascrizioni** per controllare il
percorso. Se il file e' stato spostato o rinominato fuori da StudyFlow, il record
punta ancora al vecchio percorso: ripristinare il file nel percorso originale
oppure rimuovere e riaggiungere la videolezione.

### YouTube non trova formati audio o segnala un runtime JavaScript
Aggiornare `yt-dlp` con `python -m pip install -U yt-dlp` e installare Deno o
Node.js 22+. StudyFlow rileva automaticamente `deno`, `node`, `bun` e `qjs` nel
`PATH`. Alcuni video privati, con restrizioni di eta' o geografiche non sono
scaricabili.

### Lo scorrimento con la rotella e' lento o a scatti
StudyFlow usa uno scorrimento di circa tre righe (60 pixel) per scatto e sceglie
sempre l'area piu' interna sotto il puntatore, quindi le liste annidate non
scorrono piu' insieme alla pagina che le contiene. Anche i mouse e i touchpad ad
alta risoluzione, che inviano delta piccoli, producono uno spostamento visibile.

### Non si sente l'audio
Controllare il volume di Windows, selezionare un dispositivo di output attivo e
premere **Riproduci**. Se il mixer non riesce a inizializzarsi, la vista Audio
mostra l'errore e il pulsante **Riprova**; i dettagli sono in `studyflow.log`.

### Log e diagnosi
```text
%LOCALAPPDATA%\StudyFlow\logs\studyflow.log
%LOCALAPPDATA%\StudyFlow\logs\hosts_guard.log
```

Avviare con `python main.py --debug` per il livello DEBUG.

### PDF e Slide: l'esportazione .docx non e' disponibile

Serve Pandoc. Da sorgente lo fornisce `pypandoc_binary` (in `requirements.txt`);
con l'eseguibile, `build.ps1` copia `pandoc.exe` accanto a `StudyFlow.exe`. In
alternativa si puo' installare Pandoc nel `PATH` o copiare `pandoc.exe` nella
cartella dati.

### PDF e Slide: le pagine scansionate restano vuote

Installa Tesseract-OCR con la lingua italiana oppure PaddleOCR, poi premi
**Aggiorna** nella scheda Motori: il pallino diventa verde quando il motore e'
disponibile. Con **Forza OCR su ogni pagina** si puo' ignorare il testo
vettoriale di PDF con codifica dei caratteri difettosa.

### PDF e Slide: una formula e' ricostruita male

Correggila nell'editor (l'anteprima si aggiorna subito) oppure riesegui
l'estrazione con Marker o Nougat limitati alle pagine con formule.


---

## 18. Limiti noti

- Il file hosts non e' un firewall: browser con DNS-over-HTTPS, VPN, proxy o
  risoluzione DNS alternativa possono aggirarlo. Il blocco e' pensato come aiuto
  all'attenzione, non come controllo di sicurezza inviolabile.
- Si bloccano domini, non URL o singole pagine. Sono aggiunti automaticamente
  anche i prefissi `www.` e `m.`, ma non tutti i sottodomini di servizi terzi.
- L'intercettazione dei tentativi web e' best-effort: ascolta solo le porte 80 e
  443 su `127.0.0.1` e puo' non essere disponibile se le porte sono occupate da
  altri processi.
- I tentativi ripetuti verso lo stesso target sono registrati con un cooldown
  anti-duplicati: 45 secondi per le applicazioni, 10 secondi per i siti web.
- Il monitor applicazioni confronta i nomi dei processi. Applicazioni elevate o
  processi con nomi diversi da quelli configurati possono sfuggire.
- Il blocco hosts richiede Windows e privilegi amministrativi per la modifica.
  L'app e' progettata e testata per Windows 10/11.
- L'import ICS ha limiti di dimensione, orizzonte temporale e numero di
  occorrenze per mantenere l'interfaccia reattiva.
- I dati sono locali e non sincronizzati tra PC.
- Il download YouTube dipende da `yt-dlp`, FFmpeg e dalle modifiche lato
  YouTube: un aggiornamento della piattaforma puo' richiedere un aggiornamento
  della dipendenza. Non e' disponibile alcuna riproduzione solo-audio ufficiale
  senza download.
- La trascrizione e' **solo CPU**: non usa GPU, CUDA o servizi cloud per
  l'elaborazione. La velocita' dipende dal processore, dalla durata del video e
  dal modello scelto.
- La qualita' del testo dipende da modello, lingua, rumore di fondo, microfoni e
  termini tecnici. Nomi propri, sigle e vocaboli specialistici possono essere
  trascritti in modo impreciso; il modello `small-q5_1` riduce ma non elimina
  questi errori.
- I timestamp sono associati ai blocchi di parlato (`[mm:ss]`), non alle singole
  parole. Non sono prodotti formati sottotitoli `.srt`/`.vtt` ne' la
  separazione degli interlocutori.
- Puo' essere attiva una sola trascrizione alla volta. Le trascrizioni non
  vengono accodate automaticamente.
- I modelli GGML non sono inclusi nell'exe: il primo uso richiede Internet e
  circa 60 MB (`base-q5_1`) o 190 MB (`small-q5_1`) di spazio.
- Le videolezioni non vengono copiate: se il file originale viene spostato o
  cancellato, il record resta ma la trascrizione non puo' ripartire finche' il
  percorso non torna valido.
- I `.txt` sono salvati nella cartella `transcripts`; se vengono spostati fuori
  da quella cartella, StudyFlow non li trova piu'.
- Il download di un video puo' richiedere tempo e spazio su disco; non e'
  possibile annullarlo a meta' dalla UI, ma i file temporanei vengono rimossi
  in caso di errore.
- FFmpeg non e' incluso nell'exe. Senza FFmpeg restano disponibili la musica
  locale, l'importazione dei file, i suoni di concentrazione e la consultazione
  delle trascrizioni gia' esistenti.
- La ripetizione spaziata e' una pianificazione locale: non e' sincronizzata con
  Anki o altri servizi e non usa algoritmi basati su machine learning. Gli
  intervalli dipendono esclusivamente dai giudizi inseriti dall'utente.
- Le flashcard non supportano immagini, audio, cloze o template: fronte, retro e
  note sono testo semplice.
- Il backup non include media, modelli, log o file audio. Un ripristino su un
  altro PC richiede di ricreare i percorsi di video, PDF e brani musicali.
- Il monte ore delle materie e' un riferimento calcolato con 1 CFU = 25 ore: non
  tiene conto di eventuali regolamenti didattici specifici che distinguono ore
  di lezione e studio individuale.
- La data d'esame e' gestita senza orario e senza fuso orario: il countdown
  arriva alla mezzanotte del giorno indicato. Esami con piu' appelli richiedono
  di aggiornare manualmente la data.
- La rinomina di una materia aggiorna i campi `subject` esistenti, ma non i testi
  liberi eventualmente scritti nelle note o nelle descrizioni.
- Una materia con dati collegati non puo' essere eliminata dalla vista Materie:
  e' possibile rinominarla oppure rimuovere prima materiali, video, flashcard,
  eventi e sessioni collegati.
- L'importazione e' sostitutiva e non unisce i dati: due backup non possono essere
  combinati automaticamente. Il backup di sicurezza pre-importazione consente di
  tornare allo stato precedente importando di nuovo quel file.
- Un backup creato da una versione piu' recente dello schema viene rifiutato; i
  backup della stessa versione o precedenti vengono importati con le colonne
  disponibili.
- A riposo l'app occupa circa 40-45 MB di RAM. La prima apertura della dashboard
  analitica carica Matplotlib e la prima riproduzione carica Pygame; la
  trascrizione carica il modello e il motore nativo solo nel processo di lavoro,
  quindi il consumo aumenta in modo controllato e solo quando richiesto.

---

## 19. Privacy

StudyFlow non invia dati a server remoti, con due eccezioni esplicite:

- il download YouTube richiesto dall'utente: `yt-dlp` contatta YouTube per
  leggere il video e scaricarne la traccia;
- il primo download del modello di trascrizione: `requests` contatta Hugging
  Face per scaricare il file `ggml-*.bin`. Viene inviato solo il normale traffico
  HTTP necessario al download, senza audio, trascrizioni o dati personali.

Sessioni, tentativi, materiali, eventi, file audio, videolezioni, trascrizioni,
flashcard, storico dei ripassi, modelli e impostazioni restano sul PC. L'audio
estratto per la trascrizione e'
un WAV temporaneo nella cartella temporanea di Windows, viene elaborato
localmente e cancellato al termine; nessun audio viene caricato su servizi
cloud. I file `.ics`, i PDF, i video e i brani importati vengono letti solo dal
percorso scelto dall'utente. I log contengono nomi di domini/processi bloccati,
URL YouTube, percorsi di video/trascrizioni e messaggi tecnici, e restano nella
cartella dati locale.

I backup ZIP vengono creati solo su richiesta dell'utente nella posizione scelta
e contengono dati personali in chiaro: vanno conservati e condivisi con la stessa
attenzione del database originale. StudyFlow non carica automaticamente i backup
su alcun servizio. I backup di sicurezza pre-importazione restano nella cartella
`backups` finche' non vengono eliminati manualmente.
