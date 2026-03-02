<a id="OLE_LINK1"></a><a id="OLE_LINK2"></a><a id="_Toc359077851"></a>Demonstration of DOCX support in calibre

This document demonstrates the ability of the calibre DOCX Input plugin to convert the various typographic features in a Microsoft Word \(2007 and newer\) document\. Convert this document to a modern ebook format, such as AZW3 for Kindles or EPUB for other ebook readers, to see it in action\.

There is support for images, tables, lists, footnotes, endnotes, links, dropcaps and various types of text and paragraph level formatting\.

To see the DOCX conversion in action, simply add this file to calibre using the __“Add Books” __button and then click “__Convert”\. __ Set the output format in the top right corner of the conversion dialog to EPUB or AZW3 and click __“OK”__\.

# <a id="_Toc359077852"></a>Text Formatting

## <a id="_Toc359077853"></a>Inline formatting

Here, we demonstrate various types of inline text formatting and the use of embedded fonts\.

Here is some __bold, __*italic, *__*bold\-italic, *__underlined and struck out  text\. Then, we have a superscript and a subscript\. Now we see some red, green and blue text\. Some text with a yellow highlight\. Some text in a box\. Some text in inverse video\.

A paragraph with styled text: subtle emphasis  followed by __strong text __and intense emphasis\. This paragraph uses document wide styles for styling rather than inline text properties as demonstrated in the previous paragraph — calibre can handle both with equal ease\.

## <a id="_Toc359077854"></a>Fun with fonts

This document has embedded the Ubuntu font family\. The body text is in the Ubuntu typeface, here is some text in the Ubuntu Mono typeface, notice how every letter has the same width, even i and m\. Every embedded font will automatically be embedded in the output ebook during conversion\. 

## <a id="_Paragraph_level_formatting"></a><a id="_Toc359077855"></a>__Paragraph level formatting__

You can do crazy things with paragraphs, if the urge strikes you\. For instance this paragraph is right aligned and has a right border\. It has also been given a light gray background\.

For the lovers of poetry amongst you, paragraphs with hanging indents, like this often come in handy\. You can use hanging indents to ensure that a line of poetry retains its individual identity as a line even when the screen is  too narrow to display it as a single line\. Not only does this paragraph have a hanging indent, it is also has an extra top margin, setting it apart from the preceding paragraph\.

# <a id="_Toc359077856"></a>Tables

ITEM

NEEDED

Books

1

Pens

3

Pencils

2

Highlighter

2 colors

Scissors

1 pair

Tables in Word can vary from the extremely simple to the extremely complex\. calibre tries to do its best when converting tables\. While you may run into trouble with the occasional table, the vast majority of common cases should be converted very well, as demonstrated in this section\. Note that for optimum results, when creating tables in Word, you should set their widths using percentages, rather than absolute units\.  To the left of this paragraph is a floating two column table with a nice green border and header row\.

Now let’s look at a fancier table—one with alternating row colors and partial borders\. This table is stretched out to take 100% of the available width\.

City or Town

Point A

Point B

Point C

Point D

Point E

Point A

—

Point B

87

—

Point C

64

56

—

Point D

37

32

91

—

Point E

93

35

54

43

—

Next, we see a table with special formatting in various locations\. Notice how the formatting for the header row and sub header rows is preserved\.

College

New students

Graduating students

Change

Undergraduate

Cedar University

110

103

\+7

Oak Institute

202

210

\-8

Graduate

Cedar University

24

20

\+4

Elm College

43

53

\-10

Total

998

908

90

Source: Fictitious data, for illustration purposes only

Next, we have something a little more complex, a nested table, i\.e\. a table inside another table\. Additionally, the inner table has some of its cells merged\. The table is displayed horizontally centered\.

One

Three

Two

Four

To the left is a table inside a table, with some cells merged\.

We end with a fancy calendar, note how much of the original formatting is preserved\. Note that this table will only display correctly on relatively wide screens\. In general, very wide tables or tables whose cells have fixed width requirements don’t fare well in ebooks\.

December 2007

Sun

Mon

Tue

Wed

Thu

Fri

Sat

1

2

3

4

5

6

7

8

9

10

11

12

13

14

15

16

17

18

19

20

21

22

23

24

25

26

27

28

29

30

31

# <a id="_Toc359077857"></a>Structural Elements

Miscellaneous structural elements you can add to your document, like footnotes, endnotes, dropcaps and the like\. 

## <a id="_Toc359077858"></a>Footnotes & Endnotes

Footnotes<a id="footnote-ref-2"></a>[\[1\]](#footnote-2) and endnotes<a id="endnote-ref-2"></a>[\[2\]](#endnote-2) are automatically recognized and both are converted to endnotes, with backlinks for maximum ease of use in ebook devices\.

## <a id="_Toc359077859"></a>Dropcaps

D

rop caps are used to emphasize the leading paragraph at the start of a section\. In Word it is possible to specify how many lines of text a drop\-cap should use\. Because of limitations in ebook technology, this is not possible when converting\.  Instead, the converted drop cap will use font size and line height to simulate the effect as well as possible\. While not as good as the original, the result is usually tolerable\. This paragraph has a “D” dropcap set to occupy three lines of text with a font size of 58\.5 pts\. Depending on the screen width and capabilities of the device you view the book on, this dropcap can look anything from perfect to ugly\.

## <a id="_Toc359077860"></a>Links

Two kinds of links are possible, those that refer to an external website and those that refer to locations inside the document itself\. Both are supported by calibre\. For example, here is a link pointing to the [calibre download page](http://calibre-ebook.com/download)\. Then we have a link that points back to the section on [paragraph level formatting](#_Paragraph_level_formatting) in this document\.

## <a id="_Toc359077861"></a>Table of Contents

There are two approaches that calibre takes when generating a Table of Contents\. The first is if the Word document has a Table of Contents itself\. Provided that the Table of Contents uses hyperlinks, calibre will automatically use it\. The levels of the Table of Contents are identified by their left indent, so if you want the ebook to have a multi\-level Table of Contents, make sure you create a properly indented Table of Contents in Word\.

If no Table of Contents is found in the document, then a table of contents is automatically generated from the headings in the document\. A heading is identified as something that has the Heading 1 or Heading 2, etc\. style applied to it\. These headings are turned into a Table of Contents with Heading 1 being the topmost level, Heading 2 the second level and so on\.

 You can see the Table of Contents created by calibre by clicking the Table of Contents button in whatever viewer you are using to view the converted ebook\. 

[Demonstration of DOCX support in calibre	1](#_Toc359077851)

[Text Formatting	2](#_Toc359077852)

[Inline formatting	2](#_Toc359077853)

[Fun with fonts	2](#_Toc359077854)

[Paragraph level formatting	2](#_Toc359077855)

[Tables	3](#_Toc359077856)

[Structural Elements	5](#_Toc359077857)

[Footnotes & Endnotes	5](#_Toc359077858)

[Dropcaps	5](#_Toc359077859)

[Links	5](#_Toc359077860)

[Table of Contents	5](#_Toc359077861)

[Images	7](#_Toc359077862)

[Lists	8](#_Toc359077863)

[Bulleted List	8](#_Toc359077864)

[Numbered List	8](#_Toc359077865)

[Multi\-level Lists	8](#_Toc359077866)

[Continued Lists	8](#_Toc359077867)

# <a id="_Toc359077862"></a>Images

Images can be of three main types\. Inline images are images that are part of the normal text flow, like this image of a green dot ![dot_green.png](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAFvUlEQVR42u2WfVBUVRTAz/vYt2/ZBYRVTCAEP0gFv3IEJTUFRBmpNHXGUccUGxPLsskv1JJKpbAmR0vSSSVHzRk0rWQ0FCQrERwTFdT8AmXle0Xc7/d23+u8R4sw7jM/dqb+6DJnz+Pce8/53XPuu/cRoijCv9mI/wH+UwCZR1T/ND4EZRLKBJQuKIP+tpehNKLkoRxAuf0wJ+njec8Aaw8rAgxBSdOqA+dGdI6D7oExoFH5Q2ddD7mzyXwDbHwL3LxTCpVNJ8HiuLMNzdkoZzw5W5msALAmrxVg6tC5bbbc09uytOqAJUPDp0OvrsPB6WoCHsUpmEAUHa1OCDXQpC+oqM5Ao1yrL4bTVXsQpHk9+lrazpesV01QAPjwJ0bW02JTYW/JdumfDeH6wWnx/eaBS2gAzlUPgmBHs9K+IYAkWWCorkCRQVB4cStUGc9KmViEPjn0KY9a/RLnGWD1wVaA6XGpsOfk9s3RoaPTYnumgMNpAF5oeUjgB0FUpD+o6VAouX4Iyg1F2ehzAfpsXehEBYBV3zPux6wwfd8lidEzgXPW4upNjxi4Y6OwLAzdDY6V74Jbxkvr0SSXY82rCgArctWSivFR+5ZMjp0PJGHHet99ouDupqI6gSCysL/ka7A6TLFoKl031eEZYOleGWDHiD5jZ0d26481b3yMtCuXg6G6wJXaC/Db5aM5aJiTNU0B4L3d6ggd63dj8rCZuJk4TL3tKYO7S6HBzcvA/lO7wGy/1+PzGY5KjwCLdrLLo8OiMmN6v4BpM3th9fezQBI6KL36O5TfqkjfMMv+iUeAhTvYgsRBo+LDg3oisXdW724kZqGq4TocKztRuGmOPcEjQNo3rGHKyAkhAdpAPGS4JwqkmAOCgWbLHdj3a97t7NftoR4B5m3ROGYlTGRYhsHke/eSIvDPznGws+Agt/UNm9ojQOpXGsfspFcYkMN7H0D6zcn/gdv+pgLAaxt9DNMTxoewjAo3ocurACRBYQZ42FNw5Pa3b1s9l2DGFz75KcPjxgbr9XjZ8E8USKnRpApqjEY4VHzy6O53rUkeAaZ95rP8+cjIzJi+ffAItnsVgKFZKL10Gf64ciV972Kr59dw6qfaEJ2GNcxOTsIjmMMsOL20ehqPZAZyDueD2WYPzV1maftg6QAwaZ1WUpvjhwxMi44IAxtnBW8cxRrGB8orb0HhmXPS1bzgwArL/d72AC9/LAP017Ls+dSUROx1gYN/ulKoVSyugYIdeQW4etsANF348X0FgJQMnfsxs0dw1+WTxwzDTy0r7gfHYwV1N4ZW46ebD+w/fgpu1NRLdU+X7IcyzJ4Bkj/QtZ+/aVDviLfGDx8ol0LKxKOeDdI7L61cSv2R4nNQdrXySzQvdPcf/kgBYNxKHeSvsxAZGRlETU0NVaX/bmOvZ4PmT4kfBi7gEcSCm/Phr6eKUmFgLVCggn2Fp+BadcMWqmzEOzqdzpmbmyskrdCKP69VAEhM94WR7GLSaDSq6urqpA3h3xJ2YhWj41OTYgfA4Oe640cpJ2eDc0nXtSDPo0gS73xGXjWN+uyfNyG/5DxwFiYnoPrFTEEQ7rEsa6qurnYUFRW5xHZBOwAkLPMFfWUyhYNZnucDse8ZgiCC7DpDlE1XNU7dyR7fLyIUJPH1YUHv7yvPM7aYwGS1w8VKgyyOu+xxjTn8KGsOvYzBjeijDnUTDjVjFnhFgDFLfWG0tmMGSJL0Q61BTdvZ+iCrtmqUk7kXJxJCoEDw3aR5pKiqJUSqmeb8irXW8F8Ya5cGDOpE4Vwul+mRMyABFK03t+2B2tpalZ+fH41jKMwIQdM06XQ6aXRKMwxDoCbccymKkhyhycXjGEGj0Qg4R6qRy2w28+49MHqJTjyedf8j9wEAd5NAJC3BuG0VFRVEY2MjERkZKduam5vb+gICAsTg4GARx4hRUVFtTnG+/CwFdtvaA/wF5UDY3/sPE+wAAAAASUVORK5CYII=)\. Inline images do not cause breaks in the text and are usually small in size\.![back.png](data:image/png;base64,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) The next category of image is a floating image, one that “floats “ on the page and is surrounded by text\. Word supports more types of floating images than are possible with current ebook technology, so the conversion maps floating images to simple left and right floats, as you can see with the left and right arrow images on the sides of this paragraph\.

The final type of image is a “block” image, one that becomes a paragraph on its own and has no text on either side\. Below is a centered green dot\.

![dot_green.png](data:image/png;base64,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)![forward.png](data:image/png;base64,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)Centered images like this are useful for large pictures that should be a focus of attention\. 

Generally, it is not possible to translate the exact positioning of images from a Word document to an ebook\. That is because in Word, image positioning is specified in absolute units from the page boundaries\.  There is no analogous technology in ebooks, so the conversion will usually end up placing the image either centered or floating close to the point in the text where it was inserted, not necessarily where it appears on the page in Word\.

# <a id="_Toc359077863"></a>Lists

All types of lists are supported by the conversion, with the exception of lists that use fancy bullets, these get converted to regular bullets\.

## <a id="_Toc359077864"></a>Bulleted List

- One
- Two

## <a id="_Toc359077865"></a>Numbered List

1. One, with a very long line to demonstrate that the hanging indent for the list is working correctly
2. Two

## <a id="_Toc359077866"></a>Multi\-level Lists

1. One
	1. Two
		1. Three
		2. Four with a very long line to demonstrate that the hanging indent for the list is working correctly\.
		3. Five
2. Six

A Multi\-level list with bullets:

- One
	- Two
		- This bullet uses an image as the bullet item
			- Four
- Five

## <a id="_Toc359077867"></a>Continued Lists

1. One
2. Two

An interruption in our regularly scheduled listing, for this essential and very relevant public service announcement\.

1. We now resume our normal programming
2. Four

1. <a id="footnote-2"></a> In paged media, footnotes are usually displayed at the bottom of the text\. However, in ebooks, a better paradigm is to make them clickable endnotes that the user can browse at her pleasure\. This conversion is handled automatically by calibre\. [↑](#footnote-ref-2)


2. <a id="endnote-2"></a> Endnotes are typically used for longer notes, they remain endnotes when converted into ebook form, except that they have an additional backlink to make it easy to return to the current position after reading the note\. [↑](#endnote-ref-2)



