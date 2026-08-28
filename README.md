# EV Charging Platform (Ελλάδα)

Live χάρτης δημόσιων φορτιστών EV στην Ελλάδα, με δεδομένα από το
[electrokinisi.yme.gov.gr](https://electrokinisi.yme.gov.gr) (Υπουργείο Υποδομών & Μεταφορών).

## Αρχιτεκτονική

- **Ingestion** (`scripts/ingest.py`): τρέχει σε GitHub Actions κάθε 10 λεπτά (ίδιο διάστημα με την
  ανανέωση των dynamic δεδομένων του ΥΜΕ), κατεβάζει το static +
  dynamic ZIP από το ΥΜΕ, τα ενοποιεί, και γράφει ένα συμπαγές JSON σε Cloudflare Workers KV.
- **API + frontend** (`src/worker.js`, `public/index.html`): Cloudflare Worker (Free plan) που
  σερβίρει το `/api/chargers` από το KV και τη σελίδα του χάρτη (Leaflet).

Ο Worker δεν κάνει καθόλου κατέβασμα/parsing (γι' αυτό αρκεί το δωρεάν πλάνο) — απλά διαβάζει το
ήδη επεξεργασμένο JSON από το KV.

## Βήματα εγκατάστασης (μία φορά)

### 1. Δημιούργησε το GitHub repo

Ανέβασε αυτόν τον φάκελο σε ένα νέο repo στο GitHub σου (**public repo συνιστάται** — με 10λεπτο
interval τρέχουμε ~4.320 φορές/μήνα, που σε private repo θα ξεπερνούσε εύκολα το δωρεάν όριο των
2.000 λεπτών/μήνα· σε public repo τα GitHub Actions minutes είναι απεριόριστα δωρεάν).

### 2. Cloudflare API Token

Στο Cloudflare dashboard → **My Profile → API Tokens → Create Token** → custom token με permission
**Account → Workers KV Storage → Edit**. Αντίγραψε το token.

### 3. Βρες το Account ID

Cloudflare dashboard → οποιαδήποτε σελίδα Workers/domain σου δείχνει το **Account ID** στη δεξιά
στήλη.

### 4. Πρόσθεσε GitHub Secrets

Στο repo σου: **Settings → Secrets and variables → Actions → New repository secret**, πρόσθεσε:

| Name | Value |
|---|---|
| `CF_ACCOUNT_ID` | το Account ID από το βήμα 3 |
| `CF_API_TOKEN` | το token από το βήμα 2 |
| `CF_KV_NAMESPACE_ID` | `3f2e2b122cc84aecbd0a1de405181819` |

(Το KV namespace `ev-charging-platform-cache` με αυτό το ID έχει ήδη δημιουργηθεί στο Cloudflare
account σου.)

### 5. Τρέξε το ingestion μία φορά χειροκίνητα

Στο repo: **Actions → Ingest EV charger data → Run workflow**. Ελέγχεις τα logs — πρέπει να δεις
"Payload size: ..." και "Done." στο τέλος. Μετά από αυτό, τρέχει μόνο του κάθε 10 λεπτά.

### 6. Deploy το Worker

Local, μέσα στον φάκελο του project:

```bash
npm install
npx wrangler login       # ανοίγει browser για να συνδεθείς στο Cloudflare account σου
npx wrangler deploy
```

Στο τέλος θα σου δώσει ένα link τύπου `https://ev-charging-platform.<το-subdomain-σου>.workers.dev`
— αυτό είναι το live link, ίδιο ακριβώς πνεύμα με το KAEK Navigator. Όποιος το ανοίξει βλέπει τον
χάρτη live, χωρίς καμία εγκατάσταση από τη μεριά του.

## Τοπική ανάπτυξη

```bash
npm install
npx wrangler dev
```

## Ήδη υλοποιημένα (πέρα από τον βασικό χάρτη)

- **Κοντινότεροι φορτιστές**: κουμπί "📍 Βρες κοντινούς φορτιστές" (χρήση geolocation του browser),
  ταξινομημένη λίστα με απόσταση σε km.
- **Live occupancy**: κάθε popup δείχνει "Διαθέσιμοι τώρα: X / Y" — πραγματικό στιγμιότυπο, όχι εκτίμηση.
- **Ιστορικό χρήσης (θεμέλιο)**: το `ingest.py` γράφει τώρα και ένα δεύτερο KV key
  (`usage_history`) με ένα ελαφρύ στιγμιότυπο (σύνολο/σε φόρτιση/διαθέσιμοι) σε κάθε τρέξιμο,
  κρατώντας τις τελευταίες ~2.200 καταγραφές (~30 μέρες). Δεν εμφανίζεται ακόμα πουθενά στο UI —
  είναι η βάση για ένα μελλοντικό "typical busy hours" chart, μόλις συσσωρευτούν αρκετά δεδομένα.

## Επόμενα βήματα (ιδέες)

- Route planning: OSRM API για διαδρομή Α→Β + φορτιστές πάνω στη διαδρομή.
- Σύγκριση με βάση το όχημά σου (τύπος βύσματος / μέγιστη ισχύς φόρτισης).
- Custom domain αντί για `*.workers.dev`.
- Chart με τα ιστορικά δεδομένα του `usage_history` μόλις υπάρχουν αρκετές μέρες καταγραφής.
