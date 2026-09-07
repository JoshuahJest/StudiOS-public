#import <AppKit/AppKit.h>
#import <ScreenSaver/ScreenSaver.h>

#import "CascadeMath.h"

@interface CodeCascadeSaverView : ScreenSaverView
@property(nonatomic) NSTimeInterval phase;
@end

@implementation CodeCascadeSaverView

- (instancetype)initWithFrame:(NSRect)frame isPreview:(BOOL)isPreview {
    self = [super initWithFrame:frame isPreview:isPreview];
    if (self) {
        [self setAnimationTimeInterval:(isPreview ? 1.0 / 24.0 : 1.0 / 30.0)];
        _phase = 0.0;
    }
    return self;
}

- (void)startAnimation {
    [super startAnimation];
}

- (void)animateOneFrame {
    self.phase += self.animationTimeInterval;
    [self setNeedsDisplay:YES];
}

- (NSString *)glyphAtColumn:(NSInteger)column row:(NSInteger)row {
    static NSString * const glyphs = @"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+-=<>/\\";
    NSUInteger seed = (NSUInteger)(column * 1103515245u + row * 12345u + (NSInteger)(self.phase * 24.0));
    return [glyphs substringWithRange:NSMakeRange(seed % glyphs.length, 1)];
}

- (void)drawRect:(NSRect)rect {
    [[NSColor blackColor] setFill];
    NSRectFill(rect);

    CGFloat width = NSWidth(self.bounds);
    CGFloat height = NSHeight(self.bounds);
    NSInteger layers = 3;

    for (NSInteger layer = layers - 1; layer >= 0; layer--) {
        CGFloat depth = (CGFloat)layer / (CGFloat)(layers - 1);
        CGFloat scale = cascade_scale_for_depth(depth);
        CGFloat alpha = cascade_alpha_for_depth(depth);
        CGFloat fontSize = MAX(9.0, 18.0 * scale);
        CGFloat spacing = fontSize * 0.9;
        NSInteger columns = MAX(1, (NSInteger)(width / spacing));
        NSFont *font = [NSFont monospacedSystemFontOfSize:fontSize weight:NSFontWeightMedium];
        NSDictionary *attrs = @{ NSFontAttributeName: font,
                                 NSForegroundColorAttributeName: [[NSColor whiteColor] colorWithAlphaComponent:alpha] };

        for (NSInteger column = 0; column < columns; column++) {
            CGFloat drift = sin(self.phase * (0.9 + depth) + column * 1.71) * fontSize * 2.0;
            CGFloat fall = fmod(self.phase * (40.0 + layer * 18.0) + column * 29.0, height + fontSize * 24.0);
            NSInteger tail = 10 + (column % 18);
            for (NSInteger row = 0; row < tail; row++) {
                CGFloat y = height - fall + row * fontSize;
                if (y < -fontSize || y > height + fontSize) continue;
                CGFloat tailAlpha = alpha * (1.0 - ((CGFloat)row / (CGFloat)tail) * 0.80);
                NSDictionary *tailAttrs = @{ NSFontAttributeName: font,
                    NSForegroundColorAttributeName: [[NSColor whiteColor] colorWithAlphaComponent:tailAlpha] };
                NSString *glyph = [self glyphAtColumn:column + layer * 97 row:row];
                [glyph drawAtPoint:NSMakePoint(column * spacing + drift, y) withAttributes:tailAttrs];
            }
        }
    }
}

@end
