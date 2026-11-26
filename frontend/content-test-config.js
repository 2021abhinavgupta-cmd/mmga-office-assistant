/* Content writing test: the questions and how long each one lasts.
   Edit only this file to change the test. Taken from the MMGA Proof screening test.
   Answer keys and scoring rubrics are NOT here (anyone could read this file);
   they live on the server in backend/content_test_key.py and go into the emailed sheet.

   Per question:
     id      short unique id
     sec     section letter, see `sections` below (same section = one page-spread)
     t       seconds for this question
     prompt  the question; {words} are filled from one random item of `pool`
     fields  boxes to type in: { k, label, max, rows, multi, placeholder, star, allow }
               multi = bigger box, star = "I'd shoot this one" pick, allow = paste is allowed in this box
     opts    multiple choice: [id, text]; shuffled per person unless fixed:true
     rank    items to put in order (up / down buttons)
     pool    variants, one is picked at random per person
     tiles   four mood-board image urls to pick from      split  four % boxes for a budget split
     target  the sentence to type (typing-speed question) */
window.CONTENT_TEST = {
  formId: 'content_writing_test',
  sections: {
    W: 'Warm up', P: 'The basics', A: 'Gut check', B: 'The real you', C: 'Show us the work'
  },
  questions: [
    { id: 'Q0', sec: 'W', t: 30, prompt: 'Type this sentence as fast as you can: "Vada pav at Ghatkopar station tastes better after a bad day."', target: 'Vada pav at Ghatkopar station tastes better after a bad day.', fields: [{ k: 'text', multi: true, rows: 2, placeholder: 'Start typing…' }] },
    { id: 'P1', sec: 'P', t: 45, prompt: 'Money talk, zero awkwardness. What was your last CTC, and what are you expecting?', fields: [{ k: 'cur', label: 'Last CTC (₹ per year)', max: 40, placeholder: 'e.g. 4.2 LPA, or "fresher"' }, { k: 'exp', label: 'Expected CTC (₹ per year)', max: 40, placeholder: 'e.g. 6 LPA' }] },
    { id: 'P2', sec: 'P', t: 30, prompt: 'How did you hear about MMGA?', fixed: true, opts: [['Instagram', 'Instagram'], ['LinkedIn', 'LinkedIn'], ['Friend', 'A friend, or someone at MMGA'], ['Portal', 'A job portal'], ['College', 'College placement'], ['Other', 'Somewhere else']], fields: [{ k: 'detail', label: 'Tell us more, optional', max: 140 }] },
    { id: 'A1', sec: 'A', t: 40, prompt: "It's 7:40 PM. A reel script you own is due to the client at 10 AM. You just realised the hook is weak. What do you do?", opts: [['a', 'Send it; fix in revisions.'], ['b', 'Rework it late tonight, tell no one.'], ['c', 'Message your lead now: "Hook is weak, my fix will be in by 9 AM."'], ['d', 'Wait for feedback.']], fields: [{ k: 'why', label: 'Why? One line, optional', max: 140 }] },
    { id: 'A2', sec: 'A', t: 35, prompt: 'The client says your script "isn\'t funny." You think it is. First move?', opts: [['a', 'Defend the joke.'], ['b', 'Rewrite immediately.'], ['c', "Ask what they've found funny lately and what they expected."], ['d', 'Ask your lead to handle it.']], fields: [{ k: 'why', label: 'Why? One line, optional', max: 140 }] },
    { id: 'A3', sec: 'A', t: 30, prompt: "You're done by 5 PM. The designer is drowning in a deadline. You…", opts: [['a', "Log off, you've earned it."], ['b', 'Ask "what can I take off your plate?"'], ['c', 'Wait to be asked.'], ['d', "Tell your manager they're behind."]], fields: [{ k: 'why', label: 'Why? One line, optional', max: 140 }] },
    { id: 'A4', sec: 'A', t: 30, prompt: 'A post you wrote went live with a typo. It has 3,000 views. Nobody has noticed yet.', opts: [['a', 'Quietly fix or repost it.'], ['b', 'Tell your lead and the client, propose a fix.'], ['c', "Leave it, it's small."], ['d', 'Fix it and mention it only if asked.']], fields: [{ k: 'why', label: 'Why? One line, optional', max: 140 }] },
    { id: 'A5', sec: 'A', t: 30, prompt: 'Your manager rewrites your script heavily. Honest reaction?', opts: [['a', "Annoyed, but I'll compare and learn."], ['b', 'Fine, less work.'], ['c', "I'd ask why each change was made."], ['d', "I'd feel they don't trust me."]], fields: [{ k: 'why', label: 'Why? One line, optional', max: 140 }] },
    { id: 'A6', sec: 'A', t: 30, prompt: 'Rank these from most to least like you.', rank: ['Being right', 'Being liked', 'Being fast', 'Being original'], fields: [] },
    { id: 'B1', sec: 'B', t: 60, prompt: "What's the last thing on the internet that made you actually laugh out loud? Describe it.", fields: [{ k: 'text', multi: true, rows: 5, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B2', sec: 'B', t: 45, prompt: 'Name an ad you hate. One line on why.', fields: [{ k: 'text', multi: true, rows: 3, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B3', sec: 'B', t: 40, prompt: 'Roast yourself in one line.', fields: [{ k: 'text', multi: true, rows: 2, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B4', sec: 'B', t: 60, prompt: "Why did you leave (or why are you leaving) your last job? Still studying? Tell us why you're ready to leave what you do now.", fields: [{ k: 'text', multi: true, rows: 5, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B5', sec: 'B', t: 45, prompt: 'What would your last manager or teammate say is your most annoying habit at work?', fields: [{ k: 'text', multi: true, rows: 4, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B6', sec: 'B', t: 90, prompt: 'Your biggest mistake at work, an internship or a project. What did you do in the first hour after you found out?', fields: [{ k: 'text', multi: true, rows: 6, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B7', sec: 'B', t: 60, prompt: 'What do you actually want to do in life? Not the interview answer. The real one.', fields: [{ k: 'text', multi: true, rows: 5, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B8', sec: 'B', t: 60, prompt: 'Why MMGA? Not why an agency. Why us.', fields: [{ k: 'text', multi: true, rows: 5, placeholder: 'Type it out. First instinct.' }] },
    { id: 'B9', sec: 'B', t: 40, prompt: 'Your Instagram bio is now a billboard on the Western Express Highway. What does it say?', fields: [{ k: 'text', multi: true, rows: 2, placeholder: 'Type it out. First instinct.' }] },
    { id: 'C1', sec: 'C', t: 120, prompt: 'Kill the boring hook. A brand\'s reel opens with: "{line}" Write 3 replacement opening lines, each from a different angle. Then star the one you\'d actually shoot.', pool: [{ line: 'Introducing Sharma Sweets, now delivering across Mumbai!' }, { line: 'Introducing Apex Classes, JEE coaching with expert faculty!' }, { line: "Meet Sweet Crumbs, Ahmedabad's favourite home baker!" }, { line: 'Introducing CA Talks, learn taxes the easy way!' }, { line: "Bhau's Vada Pav, now delivering in Ghatkopar!" }], fields: [{ k: 'l1', label: '1 · A relatable truth', star: true }, { k: 'l2', label: '2 · A provocation or question', star: true }, { k: 'l3', label: '3 · A visual or absurd one', star: true }] },
    { id: 'C2', sec: 'C', t: 90, prompt: 'Find the sell. Product: {product}.', pool: [{ product: 'a ₹2,999 ceiling fan' }, { product: 'a ₹499 steel water bottle' }, { product: 'a ₹1,299 mosquito repellent machine' }], fields: [{ k: 'truth', label: '(a) The one human truth that makes someone buy it', multi: true, rows: 3 }, { k: 'line', label: '(b) The single line that sells it', max: 160 }] },
    { id: 'C4', sec: 'C', t: 40, prompt: 'Which hook is better, and why (one line)?', fixed: true, opts: [['A', '"Unlock your child\'s true potential with our expert faculty."'], ['B', '"Your kid\'s tuition costs more than your car EMI. Is it working?"']], fields: [{ k: 'reason', label: 'Why, in one line', max: 160 }] },
    { id: 'C5', sec: 'C', t: 90, prompt: "Influencer negotiation. A {creator} quotes {quote} for one reel. Your budget is {budget}. Type the WhatsApp message you'd send.", pool: [{ creator: 'food creator with 180K followers', quote: '₹60,000', budget: '₹35,000' }, { creator: 'fitness creator with 320K followers', quote: '₹1,20,000', budget: '₹70,000' }, { creator: 'fashion creator with 90K followers', quote: '₹40,000', budget: '₹22,000' }], fields: [{ k: 'text', multi: true, rows: 6, placeholder: 'Hi! …' }] },
    { id: 'C6', sec: 'C', t: 120, prompt: 'Campaign split. {budget} to launch {brand} in {city} in 30 days. Split it, then give one online idea and one offline idea.', split: true, pool: [{ budget: '₹1.5 lakh', brand: 'a home baker cake brand', city: 'Ahmedabad' }, { budget: '₹2 lakh', brand: 'a new café', city: 'Bandra' }, { budget: '₹3 lakh', brand: 'a coaching institute', city: 'Pune' }], fields: [{ k: 'online', label: 'One online idea', multi: true, rows: 2 }, { k: 'offline', label: 'One offline idea', multi: true, rows: 2 }] },
    { id: 'C7', sec: 'C', t: 90, prompt: 'Shoot prep. You have a 4 hour shoot tomorrow for 3 reels. List the first 5 things you do today, in order.', fields: [1, 2, 3, 4, 5].map(n => ({ k: 's' + n, label: '0' + n, max: 120 })) },
    { id: 'C8', sec: 'C', t: 60, prompt: 'Mood board pick. Brief: "{brief}" Pick one of these 4 mood boards and say in one line what you\'d change.', tiles: ['/mood/moodboard-1.jpg', '/mood/moodboard-2.jpg', '/mood/moodboard-3.jpg', '/mood/moodboard-4.jpg'], pool: [{ brief: 'Monsoon chai launch. Cosy, warm, unhurried, made at home.' }], fields: [{ k: 'change', label: "What you'd change, in one line", max: 160 }] },
    { id: 'C9', sec: 'C', t: 240, prompt: "3 content ideas for MMGA's own Instagram, each with a reference link (a reel or post that inspired it, any brand, any country) and one line on how MMGA's version would be different.", fields: [1, 2, 3].flatMap(n => [{ k: 'idea' + n, label: 'Idea 0' + n + ': the idea', multi: true, rows: 2 }, { k: 'link' + n, label: 'Reference link · paste allowed', allow: true, placeholder: 'https://' }, { k: 'twist' + n, label: "How MMGA's version is different", max: 160 }]) },
    { id: 'C10', sec: 'C', t: 120, prompt: "In the next 5 months, where can you take MMGA's Instagram following? Give us a number and the 3 moves that get us there.", fields: [{ k: 'num', label: 'Followers by month 5', max: 60, placeholder: 'e.g. from 8K to 25K' }, { k: 'm1', label: 'Move 01', max: 160 }, { k: 'm2', label: 'Move 02', max: 160 }, { k: 'm3', label: 'Move 03', max: 160 }] }
  ]
};
