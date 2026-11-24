/* Content writing test: the questions and how long each one lasts.
   Edit only this file to change the test.

   section : questions with the same section name share one page-spread
   id      : any short unique id (letters/numbers), used to store the answer
   label   : the question the candidate sees
   hint    : optional small help under the box (can include "Example: ...")
   seconds : countdown for this question
   type    : 'textarea' (long answer) or 'text' (one line)

   The questions below are SAMPLES until the real ones are put in. */
window.CONTENT_TEST = {
  formId: 'content_writing_test',
  questions: [
    { id: 'w1', section: 'Warm-up', type: 'text', seconds: 45,
      label: 'SAMPLE: Write a catchy headline for a new coffee shop opening in Mumbai.' },
    { id: 'w2', section: 'Warm-up', type: 'textarea', seconds: 60,
      label: 'SAMPLE: Describe your favourite product in three sentences, as if selling it.' },
    { id: 'c1', section: 'Captions', type: 'textarea', seconds: 90,
      label: 'SAMPLE: Write an Instagram caption for a festive-season sale at a clothing brand.',
      hint: 'Keep it short and friendly. Example: Two or three lines with a call to action.' },
    { id: 'c2', section: 'Captions', type: 'textarea', seconds: 90,
      label: 'SAMPLE: Rewrite this to sound more exciting: "We have launched a new app."' },
    { id: 'l1', section: 'Long form', type: 'textarea', seconds: 180,
      label: 'SAMPLE: Write a short paragraph on why small businesses should invest in social media.' }
  ]
};
