# হাতে-কলমে পরীক্ষার তালিকা (Manual test checklist)

প্রজেক্ট শেষ হলে একসাথে পুরোটা পরীক্ষা করার জন্য। automated test (৫০০টা) কোড ঠিক আছে কিনা দেখে; এই তালিকা দেখে **আসল ব্যবহারে** সিস্টেম কাজ করে কিনা, বিশেষ করে আপনার নিজের আসল ফাইলে।

প্রতিটা ধাপে `[ ]`-এ ঠিক চিহ্ন দিন। ব্যর্থ হলে কী করলেন আর কী দেখলেন লিখে আমাকে পাঠান।

> অবস্থা: ✅ এখনই পরীক্ষা করা যায় · 🔑 আসল AI key লাগবে · ⏳ ফিচার এখনো বানানো হয়নি (user login, আসল semantic embedding)

---

## ০. প্রস্তুতি

**সার্ভার চালু করুন** (প্রতিটা আলাদা terminal-এ; বিস্তারিত `docs/running_locally.md`):
```bash
# Postgres (বন্ধ থাকলে) — কমান্ড running_locally.md-এ
cd "/Users/rafshedul/Desktop/Document Intelligence System/backend"
.venv/bin/uvicorn app.main:app --port 8000          # backend
cd ../frontend && npm run dev                        # frontend
```
- [ ] http://localhost:3000 খোলে, উপরে "backend offline" লেখা নেই
- [ ] http://localhost:8000/docs খোলে (Swagger)
- [ ] http://localhost:8000/health-এ `postgres: true`, `pgvector: true` (Redis `false` চলবে যদি inline মোডে থাকেন)

**পরিষ্কার শুরু:** পুরনো ডেটা সরাতে ওয়েবে প্রতিটা document Delete করুন, অথবা `psql`-এ `delete from documents;`।

---

## ১. ফাইল আপলোড ও প্রসেসিং ✅
`sample_data/` থেকে **এই ক্রমে** আপলোড করুন (একসাথে বেছে নিলেও হয়):

| ফাইল | প্রত্যাশিত ফল |
|---|---|
| `digital_invoice.pdf` | **completed**, Invoice `INV-2026-0042`, Total `757.05`, Currency `USD`, ৩টা line item |
| `scanned_invoice.pdf` | OCR হবে (confidence ~৯৪%), একই ইনভয়েস বলে **needs_review** + `DUPLICATE_INVOICE` |
| `scanned_invoice.jpg` | একই, ছবি থেকে OCR |
| `invoices.csv` | **completed**, ১৫টা ইনভয়েস কলাম থেকে সরাসরি, ৩০টা warning (currency/customer নেই) |
| `multi_sheet.xlsx` | **needs_review**, ২টা sheet (খালি sheet বাদ), LineItems sheet-এ header তথ্য নেই বলে `MISSING_FIELD` |
| `corrupted.pdf` | **failed**, পরিষ্কার বাংলা/ইংরেজি বার্তা, কোনো crash নয় |

- [ ] উপরের প্রতিটার ফল মিলেছে
- [ ] একসাথে ৪-৫টা ফাইল বেছে দিলে প্রতিটার পাশে progress bar দেখা যায়
- [ ] একই ফাইল আবার দিলে "already uploaded" বার্তা আসে, নতুন document হয় না
- [ ] `.docx` বা `.txt` দিলে "not supported" বার্তা
- [ ] `.pdf` নাম দিয়ে আসলে `.png` ছবি আপলোড (নাম বদলে) → ধরা পড়ে (extension mismatch)
- [ ] ০ বাইটের ফাইল → "empty" বার্তা
- [ ] পাসওয়ার্ড দেওয়া PDF → "password-protected" বার্তা (নিজে একটা বানিয়ে নিন: Word/Preview থেকে encrypt করে PDF)

## ২. OCR — আপনার নিজের ছবিতে ✅
ফোনে ইনভয়েসের ছবি তুলে আপলোড করুন:
- [ ] সোজা, ভালো আলো → text ঠিক, confidence ৮০%-এর উপরে
- [ ] সামান্য কাত করে তোলা → ঠিক পড়ে
- [ ] **উল্টো করে** তোলা (১৮০°) → ঠিক পড়ে
- [ ] ঝাপসা বা অন্ধকার ছবি → confidence **কম** দেখায়, আর ভুল তথ্য বানায় না (null থাকে)
- [ ] বহু পেজের PDF-স্ক্যান বা multi-page TIFF → প্রতিটা পেজ আলাদা, সব পেজ আসে
- [ ] 🔇 বাংলা লেখা ইনভয়েস → এই মেশিনে বাংলা language data নেই; Docker-এ থাকবে (⏳ Docker পরীক্ষা পর্যন্ত বাকি)

## ৩. Excel / CSV ✅
আপনার নিজের স্প্রেডশিট দিন:
- [ ] কলামের নাম `Invoice No`, `Total`, `Supplier`, `Date` হলে AI ছাড়াই সরাসরি মেলে (ব্যানারে "mapped from columns")
- [ ] একাধিক sheet, মাঝে খালি sheet → খালি বাদ
- [ ] merged cell আছে এমন ফাইল → মান সব ঘরে কপি হয়
- [ ] পুরনো `.xls` ফাইল
- [ ] CSV: Excel থেকে "CSV UTF-8" আর "CSV (comma)" দুভাবে save করে; `;` দিয়ে ভাগ করা ইউরোপীয় CSV; ইংরেজি ছাড়া অক্ষরের (বাংলা, রুশ) CSV
- [ ] ১০,০০০+ সারির বড় CSV → হ্যাং করে না

## ৪. AI extraction — সবচেয়ে গুরুত্বপূর্ণ 🔑
`backend/.env`-এ `LLM_PROVIDER=anthropic` আর `LLM_API_KEY=...` দিন, backend আবার চালান। ওয়েবের উপরের ব্যানারে model-এর নাম দেখাতে হবে ("DEMO" নয়)।

**নিজের ২০টা আসল ইনভয়েস** (ভিন্ন সরবরাহকারী, ভিন্ন ফরম্যাট, কয়েকটা স্ক্যান) আপলোড করে এই ছক ভরুন:

| # | ফাইল | invoice no ✔/✘ | date | supplier | total | currency | line items | কোনো মান *বানানো* হয়েছে? |
|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | |

- [ ] **সঠিকতা:** প্রতিটা field-এ কতগুলো সঠিক (লক্ষ্য: total ও invoice no ≥ ৯৫%)
- [ ] **কোনো বানানো মান নেই:** যে তথ্য ইনভয়েসে নেই সেটা `null` থাকে, অনুমান করে বসে না (সবচেয়ে গুরুত্বপূর্ণ)
- [ ] প্রতিটা মানের পাশে source snippet আছে আর সেটা সত্যিই ইনভয়েসে লেখা আছে
- [ ] confidence যুক্তিসঙ্গত: ঝাপসা ইনভয়েসে কম, পরিষ্কারে বেশি
- [ ] অস্পষ্ট তারিখ (`03/04/2026`) null হয়, অনুমান করা হয় না
- [ ] ২-৩টা ইনভয়েস একই PDF-এ থাকলে আলাদা আলাদা বের হয়
- [ ] একই PDF-এ ৫০+ ইনভয়েস থাকলে (বা ৫০+ পেজ) কতক্ষণ লাগে আর খরচ কত (Anthropic Console-এ দেখুন) লিখে রাখুন
- [ ] ভুল উত্তর পেলে: কোন ফাইল, কী ভুল — আমাকে পাঠান, prompt ঠিক করব

## ৫. Validation ✅
নিজের হাতে খারাপ ইনভয়েস বানান (বা কোনো ইনভয়েসে `edit` দিয়ে মান বদলান):
- [ ] Total বদলে দিন → `TOTAL_MISMATCH` (লাল), status **needs_review**
- [ ] ঠিক করে আগের মান দিন → error চলে যায়, status **completed**
- [ ] line item-এর দাম বদলান → `LINE_ITEM_MISMATCH`
- [ ] subtotal বদলান → `SUBTOTAL_MISMATCH`
- [ ] একই ইনভয়েস দুবার (ভিন্ন ফাইলে) → `DUPLICATE_INVOICE`
- [ ] একই ইনভয়েসের PDF আর Excel — মিললে কোনো error নেই; total/supplier না মিললে `CROSS_DOCUMENT`
- [ ] ভবিষ্যতের তারিখ → `INVALID_DATE` (warning)
- [ ] ঝাপসা স্ক্যান → `LOW_CONFIDENCE` (warning)
- [ ] শুধু warning থাকলে status **completed** + "flagged" ব্যাজ

## ৬. সংশোধন, Reprocess, Delete ✅
- [ ] যেকোনো field-এর পাশে `edit` → নতুন মান → সাথে সাথে validation আবার চলে, ✎ চিহ্ন আসে
- [ ] ভুল মান (`abc` টাকার ঘরে, `2026-13-45` তারিখে) → বার্তা দেখায়, কিছু বদলায় না
- [ ] `psql` দিয়ে `select * from corrections;` → পুরনো মান, নতুন মান, কখন সংরক্ষিত
- [ ] failed/needs_review document-এ **Reprocess** → আবার প্রসেস হয়; হাতে-সংশোধন করা হলে সতর্কবার্তা আসে
- [ ] completed document-এ Reprocess বোতাম নেই
- [ ] **Delete** → document, ফাইল, সব তথ্য মোছে (`ls /tmp/docintel-uploads` দিয়ে ফাইল গেছে কিনা দেখুন)

## ৭. Search ✅ (semantic অংশ ⏳)
কয়েকটা ইনভয়েস থাকা অবস্থায়:
- [ ] **keyword:** সরবরাহকারীর নাম, ইনভয়েস নম্বর, পণ্যের নাম লিখে খুঁজুন → সঠিক document, শব্দ হলুদ highlight, পেজ নম্বর
- [ ] `"exact phrase"` উদ্ধৃতি চিহ্নসহ, `শব্দ -বাদ`, `এটা OR ওটা`
- [ ] ফিল্টার: supplier, তারিখের সীমা, status — ফল কমে/বদলায়
- [ ] ফল না পেলে "0 results", error নয়
- [ ] ⏳ **semantic:** এখন শব্দের মিলে চলে, অর্থ বোঝে না ("গাড়ি" দিয়ে "vehicle" মিলবে না)। আসল embedding যোগ হলে আবার পরীক্ষা করুন
- [ ] **hybrid** ফল keyword আর semantic-এর চেয়ে যুক্তিসঙ্গত লাগে কিনা

## ৮. ব্যর্থতার মহড়া ✅ (Redis/Celery মোডে চালিয়ে)
Redis আর Celery worker চালু করে (`docs/running_locally.md`-এর শেষ অংশ), `TASK_BACKEND=celery`:
- [ ] ৩০+ পেজের স্ক্যান PDF আপলোড, কাজ চলাকালীন worker-এর terminal-এ `Ctrl+C` চেপে বা `kill -9` দিয়ে বন্ধ → নতুন worker চালু করলে কাজ শেষ হয়, পেজ ডুপ্লিকেট হয় না
- [ ] Redis বন্ধ করে (`redis-cli shutdown`) আপলোড → ~১-২ সেকেন্ডে পরিষ্কার বার্তা, ঝুলে থাকে না; তালিকা দেখা যায়
- [ ] Redis আবার চালু করে failed document-এ Reprocess → কাজ করে
- [ ] Postgres বন্ধ করে ওয়েব খুলুন → সুন্দর error, crash নয়
- [ ] ইন্টারনেট বন্ধ করে (বা ভুল API key দিয়ে) আপলোড → "LLM unavailable" বা "invalid key"-এর পরিষ্কার বার্তা; ভুল key আবার চেষ্টা করে না, নেটওয়ার্ক সমস্যায় ৩ বার চেষ্টা করে

## ৯. নিরাপত্তা ✅ (login অংশ ⏳)
`ENVIRONMENT=production` আর `API_AUTH_TOKEN` সেট করে চালান:
- [ ] ওয়েবে Sign in বক্স আসে; ভুল token → "Wrong token"
- [ ] `curl localhost:8000/api/documents` (token ছাড়া) → 401
- [ ] token ছাড়া সার্ভার চালু হয় না (production-এ) — স্পষ্ট বার্তা দেয়
- [ ] ৬০+ ফাইল এক মিনিটে আপলোড → "too many uploads" (429)
- [ ] `../../x.pdf` নামের ফাইল → শুধু `x.pdf` নাম, অন্য কোথাও লেখে না
- [ ] error বার্তায় কোনো stack trace বা ভেতরের পথ (`/Users/...`) দেখায় না
- [ ] `git status`-এ `.env` দেখা যায় না; GitHub-এ গিয়ে `.env` ফাইল নেই নিশ্চিত করুন
- [ ] ⏳ user account: আলাদা ব্যবহারকারী একে অপরের ডেটা দেখতে পায় না (এখনো বানানো হয়নি)

## ১০. গতি ও বড় ফাইল ✅
- [ ] ১০০ পেজের digital PDF → কত সেকেন্ড?
- [ ] ৫০ পেজের স্ক্যান PDF (OCR) → কত মিনিট? progress bar পেজ ধরে বাড়ে?
- [ ] ১০০০ পেজের PDF → মেমরি (Activity Monitor), মোট সময়, শেষ পর্যন্ত সফল?
- [ ] ২০০ MB-এর বেশি ফাইল → "too large" বার্তা, সার্ভার ঠিক থাকে
- [ ] ১০০০+ document থাকলে ওয়েবের তালিকা আর search কত দ্রুত?

## ১১. Docker (⏳ Docker ইনস্টল করার পর)
- [ ] `cp .env.example .env`, `API_AUTH_TOKEN` ও পাসওয়ার্ড বসান, `docker compose up --build`
- [ ] সব container healthy; migration নিজে চলে
- [ ] আপলোড → Celery worker প্রসেস করে → ফল আসে
- [ ] বাংলা OCR (ইমেজে `tesseract-ocr-ben` আছে) কাজ করে
- [ ] container restart করলে ডেটা থাকে

## ১২. শেষ অনুমোদন
| বিষয় | ফল (পাশ/ব্যর্থ) | মন্তব্য |
|---|---|---|
| ১ আপলোড ও প্রসেসিং | | |
| ২ OCR | | |
| ৩ Excel/CSV | | |
| ৪ AI সঠিকতা (≥৯৫% total/invoice no, ০টা বানানো মান) | | |
| ৫ Validation | | |
| ৬ সংশোধন/Reprocess/Delete | | |
| ৭ Search | | |
| ৮ ব্যর্থতার মহড়া | | |
| ৯ নিরাপত্তা | | |
| ১০ গতি | | |
| ১১ Docker | | |

**ব্যর্থ হলে আমাকে এটুকু পাঠান:** কোন ধাপ, কোন ফাইল, কী আশা করেছিলেন, কী দেখলেন (স্ক্রিনশট ভালো), আর terminal-এ কোনো লাল লেখা এলে সেটা।
