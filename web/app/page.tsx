import { redirect } from "next/navigation";

/**
 * There is no homepage yet, and the board is what people come for. Redirect
 * rather than render the leaderboard twice: two URLs serving the same table
 * split the links people share and the search results that index it.
 *
 * When there is real landing copy, this becomes that page.
 */
export default function Home() {
  redirect("/leaderboard");
}
