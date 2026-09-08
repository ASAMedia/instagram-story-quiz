# Instagram story quiz

Every morning a story goes out with a random detail cut from one of your old
posts and the question "when and where was this taken?". Followers reply to the
story (replies land in your DMs). Twelve hours later a second story reveals the
full photo, the posting date, the place if you have recorded one, and marks
where the crop came from.

Everything runs on GitHub Actions, for free, and uses only the official
Instagram API. No interactive stickers are involved because the API cannot
attach them.

## How it works

```
07:00 UTC  make question  -> docs/<date>-question.jpg + state/current.json
           git push       -> image becomes public on raw.githubusercontent.com
           publish        -> story goes live
19:00 UTC  make reveal    -> docs/<date>-reveal.jpg
           git push, publish
Mon 03:00  refresh        -> renews the 60-day access token
```

`state/current.json` remembers which post and which crop were chosen so the
reveal matches the question. `state/history.json` keeps the last picks so a post
is not repeated for a while (see `avoid_repeat_last_n` in `config.json`).

## One-time setup

### 1. Instagram account

Your account must be a **Professional** account (Business or Creator). Switch in
the Instagram app under Settings, Account type and tools. This is free and can
be reverted.

### 2. Meta developer app

1. Go to <https://developers.facebook.com/apps/> and create an app. Choose the
   **Business** type if asked, no Facebook Page is needed.
2. Add the **Instagram** product to the app.
3. Under the Instagram product, open **API setup with Instagram login**.
4. In the section for generating access tokens, add your Instagram account as
   an Instagram tester. Accept the invitation in the Instagram app under
   Settings, Website permissions, Tester invites.
5. Click **Generate token** next to your account, log in, and grant the
   permissions `instagram_business_basic` and
   `instagram_business_content_publish`. Copy the long-lived token. It is valid
   for 60 days and the workflow refreshes it automatically if the optional
   refresh step below is done.

The app can stay in development mode. Because the token belongs to your own
account, no App Review is needed.

### 3. GitHub repository

1. Create a **public** repository and push this folder to it. The repository
   has to be public because Instagram fetches the story image from
   `raw.githubusercontent.com`. If you prefer a private repository, host the
   `docs/` folder elsewhere (Cloudflare R2, S3, any web space) and set
   `PUBLIC_BASE_URL` in the workflow to that location.
2. Under Settings, Secrets and variables, Actions add the secret
   `IG_ACCESS_TOKEN` with the token from step 2.
3. Under Settings, Actions, General, allow workflows **read and write**
   permissions so the bot can commit the images.

### 4. Optional: automatic token refresh

The default workflow token cannot write repository secrets. Create a
fine-grained personal access token with **Secrets: read and write** for this
one repository and store it as the secret `SECRETS_WRITER_PAT`. Without it, the
weekly refresh job just prints a warning and you have to paste a fresh token
into `IG_ACCESS_TOKEN` every 60 days.

### 5. Fill in the places

In the Actions tab pick the workflow, click **Run workflow**, choose
`locations`. It reads the tag of every post and commits `locations.json`
(about 30 minutes for 850 posts). If Instagram blocks GitHub's servers, the run
stops early; then run it from home instead:

```bash
pip install -r requirements.txt
set IG_ACCESS_TOKEN=<your token>
python import_locations.py
```

and commit and push `locations.json`.

### 6. Test it

In the Actions tab pick the workflow, click **Run workflow**, choose `question`.
Check your story. Then run it again with `reveal`.

## Pausing and manual runs

Your own stories and the quiz do not conflict: Instagram simply shows them one
after another. If you still want a quiet day or week, there are three levels.

- **Pause new questions.** In the repository open Settings, Secrets and
  variables, Actions, tab **Variables**, and create `QUIZ_PAUSED` with the
  value `true`. Delete the variable or set it to `false` to resume. This works
  from the phone browser. An already published question is still revealed in
  the evening, so nobody is left hanging.
- **Pause until a date.** Create the variable `QUIZ_PAUSED_UNTIL` with a date
  like `2026-09-14`. The quiz sleeps through that day and resumes on its own
  the next morning. No need to remember to switch it back on.
- **Stop everything.** In the Actions tab pick the workflow, open the menu
  behind the three dots, and choose **Disable workflow**. Nothing runs, not
  even the reveal or the token refresh, until you enable it again.

- **Run something by hand.** In the Actions tab pick the workflow, click
  **Run workflow**, and choose `question` or `reveal`. Manual runs ignore the
  pause switch, so you can post one round during a pause.

## Adjusting things

- **Posting times**: edit the three `cron` lines in
  `.github/workflows/story-quiz.yml`. They are in UTC and do not follow daylight
  saving time. The case statement in the "Decide which step to run" step has to
  match the same strings.
- **Texts and language**: `config.json` has `"language": "de"`. Set it to `"en"`
  or override any string under `"texts"`. The defaults live at the top of
  `story_quiz.py`.
- **Difficulty**: `crop_fraction_min` and `crop_fraction_max` control how big the
  crop is relative to the photo's shorter side. Smaller is harder. The script
  samples eight random crops and keeps the one with the most visual detail, so
  plain sky or blank walls are avoided.
- **Look**: `accent` in `config.json` is the one highlight colour (pill, labels,
  crop outline). The handle and profile picture come from the API; set
  `handle` in `config.json` to override the name shown. Typography is Inter,
  bundled in `fonts/` (SIL Open Font License).
- **Places**: the official API does not expose location tags, but the public
  post page does. The daily run looks up the tag of the chosen post once and
  caches it in `locations.json`. To fill the file for all posts up front, run
  this once on your PC after the token exists (about 30 minutes for 850 posts,
  it pauses two seconds between posts and can be resumed any time):

  ```bash
  python import_locations.py
  ```

  Entries can be edited by hand, for example to turn "Kitz-Ski" into
  "Kitzbühel, Österreich". Posts stored as `null` have no tag and only reveal
  the date. If Instagram ever blocks the lookup from GitHub's servers, the
  reveal simply falls back to the date, and the importer run from home still
  works.
- **Preview locally without Instagram**:

  ```bash
  pip install -r requirements.txt
  python story_quiz.py demo path/to/any/photo.jpg _asa_g path/to/avatar.jpg "Zermatt, Schweiz"
  ```

  Handle, avatar and place are optional. This writes `docs/demo-question.jpg`
  and `docs/demo-reveal.jpg`.

## Design notes

The layout follows Instagram's story guidelines: the top 250 px and the bottom
250 px stay free of essential content because the app overlays the profile bar
and the reply field there. The call to action sits directly above the reply
field it points to. One accent colour, one type family, rounded cards with a
soft shadow on a blurred version of the photo itself, so every story matches
its picture without ever hiding it.

## Limits worth knowing

- Stories via API are plain images. Polls, quizzes and question stickers cannot
  be added. The call to action is baked into the image; replies arrive as DMs.
- The API only sees posts made after the account became a professional
  account plus older feed posts. Reels and videos are skipped; carousels are
  used (one random image out of the set).
- Scheduled GitHub workflows can start a few minutes late during busy periods.
