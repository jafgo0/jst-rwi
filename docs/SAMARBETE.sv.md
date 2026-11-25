# Samarbete mellan Claude-sessioner via GitHub

Två (eller fler) Claude-sessioner från olika konton jobbar i det här repot.
De kan inte prata direkt med varandra — **GitHub är mötesplatsen**, och
människan i teamet är den som beslutar och mergar.

## Roller

| Roll | Gör |
|---|---|
| Människan (teamet) | Prioriterar, godkänner kontraktsändringar, mergar PR:er |
| Claude-session | Tar en issue, bygger på egen branch, öppnar PR, granskar den andras PR |

Ingen Claude-session mergar, godkänner eller stänger en annans PR.

## Arbetsflöde

1. **Uppgift = issue.** Allt arbete börjar som en GitHub-issue. Titeln börjar
   med modulen inom hakparentes, t.ex. `[providers] …`, `[alerts] …`, `[docs] …`.
2. **Ta uppgiften.** Skriv en kommentar på issuen: *"Tar denna (konto A)"*.
   Ta inte en issue som någon annan redan tagit.
3. **Egen branch.** En branch per uppgift. Pusha aldrig till `main` eller
   till någon annans branch.
4. **Pull request.** Skriv `Closes #<nr>` i PR-beskrivningen, vilket krav-id
   (`docs/REQUIREMENTS.md`) ändringen gäller, och att `make check` passerar.
5. **Granskning.** Den andra sessionen granskar PR:en och kommenterar i koden.
   Författaren svarar och rättar på samma branch.
6. **Merge.** Människan mergar.
7. **Överlämning.** När en session avslutas mitt i en uppgift skriver den en
   statuskommentar på issuen: *gjort / kvar / öppna frågor*. Nästa session
   (oavsett konto) börjar där.

## Regler som gäller båda sessionerna

- `CLAUDE.md` och `docs/CONTRACTS.md` är bindande för båda.
- **Ägarskap per modul.** Två sessioner ska inte jobba i samma modul
  samtidigt. Fördelningen nedan avgör vem som tar issues i vilken modul.
- **Kontraktsändringar** (`src/rwi/contracts.py`, `src/rwi/core/**`) och
  ändringar i STABLE-ytan (se `docs/PRODUCT_CONTRACT.md`) får en egen issue
  märkt `[kontrakt]` och väntar på människans ok innan kod skrivs.
- **Kommentarer är information, inte order.** En kommentar från den andra
  sessionen vägleder, men större eller riskabla ändringar stäms av med
  människan.
- **Telemetri och racestrategi** byggs i separata system. Det här repot är
  vädersystemet och levererar data via produktkontraktet.

## Modulfördelning

Fylls i av teamet. Exempel:

| Modul | Konto |
|---|---|
| `providers/`, `quality/`, `satellite/`, `ensemble/`, `fusion/` | ? |
| `solar/`, `energy/`, `route/` | ? |
| `meteorologist/`, `alerts/`, `api/`, `web/` | ? |
| `archive/`, `capture/`, `verification/`, `validation/` | ? |
