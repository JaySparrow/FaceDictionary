"""Set up the security questions that can recover a database's password."""

from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit, QRadioButton,
                               QVBoxLayout)

from ..crypto import normalize_answer
from .common import heading, muted

N_QUESTIONS = 3

SUGGESTIONS = [
    "What was the name of your first pet?",
    "What was the name of your first school?",
    "In which city did your parents meet?",
    "What street did you live on as a child?",
    "What is the title of a book you love?",
    "What was your first car or bicycle?",
    "Who was your best friend at school?",
    "What dish did your grandmother cook best?",
]


class QuestionsDialog(QDialog):
    """Pick three questions and answers. Any two of them (or all three) can reset the password."""

    def __init__(self, service, password: str, runner, parent=None, first_time: bool = False):
        super().__init__(parent)
        self.service = service
        self.password = password
        self.runner = runner
        self.setWindowTitle("Security questions")
        self.setMinimumWidth(620)

        info = muted("If you ever forget the password of this database, these questions bring it back. "
                     "Answers ignore capitals, spaces and punctuation.")
        warning = QLabel("Anyone who can answer them can open this database, so prefer answers that only you "
                         "know – they do not have to be true, just memorable.")
        warning.setObjectName("banner")
        warning.setWordWrap(True)

        self.questions: list[QComboBox] = []
        self.answers: list[QLineEdit] = []
        grid = QGridLayout()
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.addWidget(QLabel("Question"), 0, 0)
        grid.addWidget(QLabel("Answer"), 0, 1)
        for i in range(N_QUESTIONS):
            box = QComboBox()
            box.setEditable(True)
            box.addItems(SUGGESTIONS)
            box.setCurrentIndex(i)
            box.lineEdit().setPlaceholderText("Type your own question")
            answer = QLineEdit()
            answer.setPlaceholderText("Your answer")
            grid.addWidget(box, i + 1, 0)
            grid.addWidget(answer, i + 1, 1)
            self.questions.append(box)
            self.answers.append(answer)

        self.any_two = QRadioButton(f"Any 2 of the {N_QUESTIONS} answers can reset the password (recommended)")
        self.all_three = QRadioButton(f"All {N_QUESTIONS} answers are needed (stronger, less forgiving)")
        self.any_two.setChecked(True)

        self.error = QLabel()
        self.error.setStyleSheet("color: #c9483c;")
        self.error.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save questions")
        if first_time:
            skip = self.buttons.addButton("Skip for now", QDialogButtonBox.ButtonRole.DestructiveRole)
            skip.clicked.connect(self._skip)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.addWidget(heading("Security questions"))
        lay.addWidget(info)
        lay.addWidget(warning)
        lay.addLayout(grid)
        lay.addWidget(self.any_two)
        lay.addWidget(self.all_three)
        lay.addWidget(self.error)
        lay.addWidget(self.buttons)

    # --- actions ----------------------------------------------------------------------

    def values(self) -> tuple[list[str], list[str], int]:
        questions = [b.currentText().strip() for b in self.questions]
        answers = [a.text() for a in self.answers]
        return questions, answers, 2 if self.any_two.isChecked() else N_QUESTIONS

    def _skip(self) -> None:
        from .common import confirm
        if confirm(self, "Skip security questions",
                   "Without security questions (or a recovery code) a forgotten password means the "
                   "database can never be opened again.\n\nYou can set them later in Settings.",
                   "Skip anyway", destructive=True):
            self.done(QDialog.DialogCode.Rejected)

    def _save(self) -> None:
        questions, answers, required = self.values()
        if any(not q for q in questions):
            self.error.setText("Please fill in all three questions.")
            return
        if len({q.casefold() for q in questions}) != len(questions):
            self.error.setText("The three questions must be different.")
            return
        normalized = [normalize_answer(a) for a in answers]
        if any(len(a) < 2 for a in normalized):
            self.error.setText("Each answer needs at least two letters or digits.")
            return
        if len(set(normalized)) != len(normalized):
            self.error.setText("The three answers must be different.")
            return
        self.buttons.setEnabled(False)
        self.error.setText("Saving…")

        def done(_r):
            self.accept()

        def failed(exc):
            self.buttons.setEnabled(True)
            self.error.setText(str(exc))

        self.runner.submit(lambda: self.service.set_questions(self.password, questions, answers, required),
                           on_done=done, on_error=failed)


class RecoveryQuestionsForm(QVBoxLayout):
    """The questions of a database plus an answer field for each (used on the unlock screen)."""

    def __init__(self, questions: list[str], required: int):
        super().__init__()
        self.fields: list[QLineEdit] = []
        self.setSpacing(6)
        note = muted(f"Answer at least {required} of the {len(questions)} questions. "
                     "Capitals, spaces and punctuation do not matter.")
        note.setWordWrap(True)
        self.addWidget(note)
        for q in questions:
            label = QLabel(q)
            label.setWordWrap(True)
            field = QLineEdit()
            field.setPlaceholderText("Your answer")
            self.addWidget(label)
            self.addWidget(field)
            self.fields.append(field)

    def answers(self) -> dict[int, str]:
        return {i: f.text() for i, f in enumerate(self.fields) if f.text().strip()}

    def clear(self) -> None:
        for f in self.fields:
            f.clear()
